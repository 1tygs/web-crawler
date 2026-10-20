"""What a response's body is: its Content-Type (DESIGN.md §2.4, §5.2).

The algorithms are the WHATWG ones browsers use, step for step: "parse a MIME type" from
MIME Sniffing, and "extract a MIME type" and "collect an HTTP quoted string" from Fetch.
"""

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
