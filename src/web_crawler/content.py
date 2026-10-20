"""What a response's body is: its Content-Type and its charset (DESIGN.md §2.4, §5.2, §5.5).

The algorithms are the WHATWG ones browsers use, step for step: "parse a MIME type" from
MIME Sniffing, "extract a MIME type" and "collect an HTTP quoted string" from Fetch, and,
from HTML, how a page's character encoding is determined.
"""

import codecs
import enum
import re
from typing import NamedTuple

from web_crawler.encodings import UTF_8, WINDOWS_1252, get_encoding, sniff_bom

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

_HTTP_WHITESPACE = "\t\n\r "
_HTTP_TAB_OR_SPACE = "\t "
_TOKEN = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_QUOTED_STRING_TOKEN = frozenset(
    "\t" + "".join(map(chr, range(0x20, 0x7F))) + "".join(map(chr, range(0x80, 0x100)))
)

type MimeType = tuple[str, dict[str, str]]  # essence, parameters


def parse_content_type(value: str | None) -> tuple[str | None, str | None]:
    """The essence (type/subtype, lowercase) and charset parameter of a Content-Type header
    value, or (None, None) when it's missing or unparseable. value may be several header
    lines combined with commas, as a server that sends Content-Type twice produces."""
    if value is None:
        return None, None
    found: MimeType | None = None
    essence: str | None = None
    charset: str | None = None
    for item in _split_header_value(value):
        mime = parse_mime_type(item)
        if mime is None or mime[0] == "*/*":
            continue
        found = mime
        if mime[0] != essence:
            essence = mime[0]
            charset = mime[1].get("charset")
        elif "charset" not in mime[1] and charset is not None:
            found[1]["charset"] = charset
    if found is None:
        return None, None
    return found[0], found[1].get("charset")


def parse_mime_type(text: str) -> MimeType | None:
    """WHATWG "parse a MIME type": the essence and parameters, or None on failure."""
    text = text.strip(_HTTP_WHITESPACE)
    slash = text.find("/")
    if slash == -1 or not _is_token(type_ := text[:slash]):
        return None
    semicolon = text.find(";", slash + 1)
    end = semicolon if semicolon != -1 else len(text)
    if not _is_token(subtype := text[slash + 1 : end].rstrip(_HTTP_WHITESPACE)):
        return None
    return f"{type_.lower()}/{subtype.lower()}", _parse_parameters(text, end)


def _parse_parameters(text: str, position: int) -> dict[str, str]:
    """The parameters that follow the essence, from the ";" at position."""
    parameters: dict[str, str] = {}
    while position < len(text):
        position += 1  # past ";"
        while position < len(text) and text[position] in _HTTP_WHITESPACE:
            position += 1
        start = position
        while position < len(text) and text[position] not in ";=":
            position += 1
        name = text[start:position].lower()
        if position < len(text):
            if text[position] == ";":
                continue
            position += 1  # past "="
        if position >= len(text):
            break
        if text[position] == '"':
            value, position = _collect_quoted_string(text, position, extract_value=True)
            while position < len(text) and text[position] != ";":
                position += 1
        else:
            start = position
            while position < len(text) and text[position] != ";":
                position += 1
            value = text[start:position].rstrip(_HTTP_WHITESPACE)
            if not value:
                continue
        if _is_token(name) and name not in parameters and set(value) <= _QUOTED_STRING_TOKEN:
            parameters[name] = value
    return parameters


def _is_token(text: str) -> bool:
    return bool(text) and all(c in _TOKEN for c in text)


def _collect_quoted_string(text: str, position: int, *, extract_value: bool) -> tuple[str, int]:
    """Fetch's "collect an HTTP quoted string" from the '"' at position: the value, and the
    position after it."""
    start = position
    value: list[str] = []
    position += 1
    while True:
        run_start = position
        while position < len(text) and text[position] not in '"\\':
            position += 1
        value.append(text[run_start:position])
        if position >= len(text):
            break
        quote_or_backslash = text[position]
        position += 1
        if quote_or_backslash == "\\":
            if position >= len(text):
                value.append("\\")
                break
            value.append(text[position])
            position += 1
        else:
            break
    return ("".join(value) if extract_value else text[start:position]), position


def _split_header_value(value: str) -> list[str]:
    """Fetch's "get, decode, and split": the comma-separated items, with commas inside
    quoted strings kept."""
    items: list[str] = []
    current: list[str] = []
    position = 0
    while True:
        start = position
        while position < len(value) and value[position] not in '",':
            position += 1
        current.append(value[start:position])
        if position < len(value) and value[position] == '"':
            quoted, position = _collect_quoted_string(value, position, extract_value=False)
            current.append(quoted)
            if position < len(value):
                continue
        items.append("".join(current).strip(_HTTP_TAB_OR_SPACE))
        current = []
        if position >= len(value):
            return items
        position += 1  # past ","


class Source(enum.Enum):
    """Where a page's encoding came from."""

    BOM = "bom"
    HEADER = "header"  # the Content-Type header's charset
    META = "meta"  # a <meta> in the page
    GUESS = "guess"  # nothing declared one


class Charset(NamedTuple):
    encoding: str  # an encodings.py name
    source: Source

    @property
    def tentative(self) -> bool:
        """Whether a <meta> the parser reaches can still change it (HTML Standard)."""
        return self.source in (Source.META, Source.GUESS)


def pick_charset(body: bytes, http_charset: str | None, *, truncated: bool = False) -> Charset:
    """The encoding a browser starts decoding an HTML page with.

    This is the HTML Standard's "determine the character encoding", in its order: a byte
    order mark; the Content-Type header's charset, if it names an encoding; a <meta> in the
    first 1,024 bytes; and otherwise a guess, UTF-8 if the bytes are valid UTF-8 and
    windows-1252 if they aren't. When body was cut short, a character cut off at its end
    doesn't count against UTF-8. A tentative result can still change: parse.py looks for a
    later <meta>, as a browser's parser does (meta_encoding and change_encoding).
    """
    if (bom := sniff_bom(body)) is not None:
        return Charset(bom[0], Source.BOM)
    if http_charset is not None and (encoding := get_encoding(http_charset)) is not None:
        return Charset(encoding, Source.HEADER)
    if (encoding := prescan(body[:PRESCAN_BYTES])) is not None:
        return Charset(encoding, Source.META)
    utf_8 = body.isascii() or _is_utf8(body, final=not truncated)
    return Charset(UTF_8 if utf_8 else WINDOWS_1252, Source.GUESS)


def meta_encoding(charset: str | None, http_equiv: str | None, content: str | None) -> str | None:
    """The encoding a <meta> element declares with these attributes, if any, by the HTML
    Standard's rules for a <meta> start tag: charset, or else http-equiv="Content-Type" with a
    charset in content."""
    if charset is not None and (encoding := get_encoding(charset)) is not None:
        return encoding
    if http_equiv is not None and content is not None and _is_content_type(http_equiv):
        return _charset_from_content(content.translate(_ASCII_LOWERCASE))
    return None


def change_encoding(current: str, new: str) -> str | None:
    """The HTML Standard's "change the encoding": what to re-decode a page in, now that a
    <meta> declares new, or None to carry on with current."""
    if current in _UTF_16:  # declaring it in ASCII proves it wrong, so it stays
        return None
    new = _DECLARED_AS.get(new, new)
    return None if new == current else new


def _is_content_type(value: str) -> bool:
    return value.translate(_ASCII_LOWERCASE) == "content-type"


def _is_utf8(body: bytes, *, final: bool) -> bool:
    """Whether body is UTF-8; unless final, a character cut off at the end is allowed."""
    try:
        codecs.getincrementaldecoder("utf-8")().decode(body, final=final)
    except UnicodeDecodeError:
        return False
    return True


_ASCII_LOWERCASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_UTF_16 = ("UTF-16BE", "UTF-16LE")
# An ASCII-compatible page can't be UTF-16, whatever it says, and x-user-defined is only
# for scripts.
_DECLARED_AS = {"UTF-16BE": UTF_8, "UTF-16LE": UTF_8, "x-user-defined": WINDOWS_1252}

PRESCAN_BYTES = 1024  # how far the prescan looks, as the HTML Standard encourages

# The patterns below follow the HTML Standard's "prescan a byte stream to determine its
# encoding" (§13.2.3.2) and its "get an attribute" steps. Possessive quantifiers keep a
# name or value from giving back bytes, which the standard's steps never do.
_WS = rb"\t\n\x0c\r "  # ASCII whitespace
_META = re.compile(rb"<meta[" + _WS + rb"/]", re.IGNORECASE)
_TAG = re.compile(rb"</?[A-Za-z][^" + _WS + rb">]*+")
_ATTRIBUTE = re.compile(
    rb"[" + _WS + rb"/]*+(?:(?P<end>(?=>))|(?P<name>[^" + _WS + rb"/>][^" + _WS + rb"/=>]*+)"
    rb"(?:[" + _WS + rb"]*+=[" + _WS + rb"]*+"
    rb"""(?:"(?P<double>[^"]*+)"|'(?P<single>[^']*+)'"""
    rb"|(?P<unquoted>[^" + _WS + rb""">"'][^""" + _WS + rb">]*+)(?=[" + _WS + rb">])|(?=>))"
    rb"|[" + _WS + rb"]*+(?=[^" + _WS + rb"=])))"
)


def prescan(data: bytes) -> str | None:
    """The encoding that data, the start of a page, declares in a <meta>, or None.

    This is the HTML Standard's prescan: comments and the attributes of other tags are
    skipped, so that a "<meta" inside them doesn't count, and a <meta> declares an encoding
    with charset, or with content="...; charset=..." beside http-equiv="Content-Type". If
    data ends before a tag does, the tag isn't used.
    """
    if data.startswith(b"<\x00?\x00"):  # an XML declaration in UTF-16, without a BOM
        return "UTF-16LE"
    if data.startswith(b"\x00<\x00?"):
        return "UTF-16BE"
    position = data.find(b"<")
    while position != -1:
        if data.startswith(b"<!--", position):
            # The comment ends at "-->", whose dashes can be those of "<!--".
            position = data.find(b"-->", position + 2)
            position = -1 if position == -1 else position + 2
        elif _META.match(data, position):
            encoding, position = _prescan_meta(data, position + 5)
            if encoding is not None:
                return encoding
        elif tag := _TAG.match(data, position):
            position = _skip_attributes(data, tag.end())
        elif data.startswith((b"<!", b"</", b"<?"), position):
            position = data.find(b">", position + 1)
        if position == -1:  # data ended inside a comment or a tag
            return None
        position = data.find(b"<", position + 1)
    return None


def _skip_attributes(data: bytes, position: int) -> int:
    """The position of the ">" that ends the attributes at position, or -1."""
    while (attribute := _ATTRIBUTE.match(data, position)) is not None:
        position = attribute.end()
        if attribute["end"] is not None:
            return position
    return -1


def _prescan_meta(data: bytes, position: int) -> tuple[str | None, int]:
    """The encoding a <meta> declares, if any, and the position of the ">" that ends it, or
    -1 if data ends before it does."""
    names: set[bytes] = set()
    got_pragma = False
    need_pragma: bool | None = None
    charset: str | None = None
    unknown_charset = False  # a charset attribute named no encoding
    while (attribute := _ATTRIBUTE.match(data, position)) is not None:
        position = attribute.end()
        if attribute["end"] is not None:
            break
        name = attribute["name"].lower()
        if name in names:  # only the first of each counts
            continue
        names.add(name)
        value = (attribute["double"] or attribute["single"] or attribute["unquoted"] or b"").lower()
        if name == b"http-equiv":
            if value == b"content-type":
                got_pragma = True
        elif name == b"content":
            encoding = _charset_from_content(value.decode("latin-1"))
            if encoding is not None and charset is None and not unknown_charset:
                charset, need_pragma = encoding, True
        elif name == b"charset":
            charset = get_encoding(value.decode("latin-1"))
            unknown_charset, need_pragma = charset is None, False
    else:
        return None, -1
    if need_pragma is None or (need_pragma and not got_pragma) or charset is None:
        return None, position
    return _DECLARED_AS.get(charset, charset), position


def _charset_from_content(content: str) -> str | None:
    """The HTML Standard's "extract a character encoding from a meta element", from a
    lowercased content attribute such as "text/html; charset=utf-8"."""
    position = 0
    while (found := content.find("charset", position)) != -1:
        position = _skip_ascii_whitespace(content, found + len("charset"))
        if not content.startswith("=", position):
            continue
        position = _skip_ascii_whitespace(content, position + 1)
        if position == len(content):
            return None
        if (quote := content[position]) in "\"'":
            close = content.find(quote, position + 1)
            return None if close == -1 else get_encoding(content[position + 1 : close])
        end = position
        while end < len(content) and content[end] not in ";\t\n\f\r ":
            end += 1
        return get_encoding(content[position:end])
    return None


def _skip_ascii_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position] in "\t\n\f\r ":
        position += 1
    return position
