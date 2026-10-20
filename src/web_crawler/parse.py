"""A page in, its output record and in-scope links out (DESIGN.md §5.5).

parse_page is a pure function: bytes in, bytes and strings out, no I/O and no shared state.
That's what lets the same code run inline on the event loop or in a pool of processes.
"""

import re

import ada_url
from selectolax.lexbor import LexborHTMLParser, LexborNode

from web_crawler.content import (
    UNDECODABLE,
    Charset,
    ContentDecodeError,
    Source,
    change_encoding,
    decode_body,
    meta_encoding,
    pick_charset,
)
from web_crawler.encodings import UTF_8, decode
from web_crawler.types import ParseJob, ParseResult, WorkerContext
from web_crawler.urls import dedupe_key

# The elements that hold a page's links, as one selector list, which lexbor matches in
# document order. Navigational links are printed and crawled (DESIGN.md §2.4); --all-urls
# prints every URL-bearing attribute too. Templates come along so that a declarative shadow
# root's links are read where it is. :is() groups an element's attributes: lexbor returns an
# element once for each selector in the list it matches.
_SHADOW_ROOT = "template[shadowrootmode]"
_NAVIGATIONAL = (
    'a[href], area[href], iframe[src], frame[src], link[href][rel], meta[http-equiv="refresh" i]'
    f"[content], {_SHADOW_ROOT}"
)
_ALL_URLS = (
    "a:is([href], [ping]), area:is([href], [ping]), iframe[src], frame[src],"
    ' link:is([href], [imagesrcset]), meta[http-equiv="refresh" i][content],'
    " img:is([src], [srcset]), source:is([src], [srcset]), script[src], embed[src],"
    " video:is([src], [poster]), audio[src], track[src], input:is([src], [formaction]),"
    " object[data], form[action], button[formaction], blockquote[cite], q[cite], del[cite],"
    f" ins[cite], body[background], {_SHADOW_ROOT}"
)
# What --all-urls reads from the other elements, in order. Each names one URL, except a
# srcset, which lists several. An empty src, poster, data or background names none, while
# an empty action or formaction means the page's own URL, and an empty cite the base URL.
_URL_ATTRIBUTES = {
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
    "script": ("src",),
    "embed": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "track": ("src",),
    "input": ("src", "formaction"),
    "object": ("data",),
    "form": ("action",),
    "button": ("formaction",),
    "blockquote": ("cite",),
    "q": ("cite",),
    "del": ("cite",),
    "ins": ("cite",),
    "body": ("background",),
}
_SRCSET = frozenset({"srcset", "imagesrcset"})
_HYPERLINKS = frozenset({"a", "area"})
_FRAMES = frozenset({"iframe", "frame"})
_ACTION = frozenset({"action", "formaction"})
_HYPERLINK_RELS = frozenset({"canonical", "alternate", "next", "prev"})

_HTTP = ("http:", "https:")
_join_url = ada_url.join_url
_IGNORED_BASES = ("data:", "javascript:")
_ASCII_WHITESPACE = "\t\n\f\r "
_SPLIT_ASCII_WHITESPACE = re.compile("[\t\n\f\r ]+").split
_ASCII_LOWERCASE = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

# DOM's valid shadow host names: these elements and custom elements.
_SHADOW_HOSTS = frozenset(
    {"article", "aside", "blockquote", "body", "div", "footer", "h1", "h2", "h3", "h4", "h5"}
    | {"h6", "header", "main", "nav", "p", "section", "span"}
)
_PCEN_CHAR = (
    r"[-._0-9a-z\u00b7\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u037d\u037f-\u1fff\u200c\u200d\u203f\u2040"
    r"\u2070-\u218f\u2c00-\u2fef\u3001-\ud7ff\uf900-\ufdcf\ufdf0-\ufffd\U00010000-\U000effff]"
)
_CUSTOM_ELEMENT_NAME = re.compile(f"[a-z]{_PCEN_CHAR}*-{_PCEN_CHAR}*").fullmatch
_RESERVED_NAMES = frozenset(
    {"annotation-xml", "color-profile", "font-face", "font-face-src", "font-face-uri"}
    | {"font-face-format", "font-face-name", "missing-glyph"}
)

_DECLARING_META = "meta[charset], meta[http-equiv][content]"
_UTF_8_BOM = b"\xef\xbb\xbf"


def parse_page(job: ParseJob, ctx: WorkerContext) -> ParseResult:
    """Every link on the page, resolved as a browser resolves it, in document order.

    The body is decompressed first, to at most ctx.max_bytes. A body that can't be is an
    error, and nothing is printed for it.
    """
    try:
        body, cut = decode_body(job.body, job.content_encoding, ctx.max_bytes)
    except ContentDecodeError:
        return ParseResult(b"", [], 0, 0, UNDECODABLE)
    tree, _ = parse_html(body, job.http_charset, truncated=job.truncated or cut)
    links = _Links(job.url, _base_url(tree, job.url), ctx)
    links.extract(tree)
    printed = links.printed
    record = "".join([job.url, "\n", *[f"  {url}\n" for url in printed], "\n"]).encode()
    return ParseResult(record, list(links.followed.items()), len(printed), len(body), None)


def _base_url(tree: LexborHTMLParser, url: str) -> str:
    """The HTML Standard's document base URL: the first <base href>, resolved against the
    page's own URL, unless it fails to parse or is a data: or javascript: URL."""
    if (base := tree.css_first("base[href]")) is None:
        return url
    try:
        resolved = ada_url.join_url(url, base.attrs.get("href") or "")
    except ValueError:
        return url
    return url if resolved.startswith(_IGNORED_BASES) else resolved


class _Links:
    """A page's links, collected in document order: what to print, and what to crawl.

    _add runs for every URL on a page, hundreds of times, so it keeps to a few operations;
    the dicts keep each URL's first occurrence, in order (DESIGN.md §5.5)."""

    __slots__ = (
        "_all_urls",
        "_base_url",
        "_document_url",
        "_refreshed",
        "_scope",
        "_selector",
        "followed",
        "printed",
    )

    def __init__(self, document_url: str, base_url: str, ctx: WorkerContext) -> None:
        self._document_url = document_url
        self._base_url = base_url
        self._scope = ctx.scope_prefixes
        self._all_urls = ctx.all_urls
        self._selector = _ALL_URLS if ctx.all_urls else _NAVIGATIONAL
        self.printed: dict[str, None] = {}  # display URLs
        self.followed: dict[str, str] = {}  # in-scope navigational links: key to display URL
        self._refreshed = False  # a meta refresh took effect; any later one is ignored

    def extract(self, root: LexborHTMLParser | LexborNode) -> None:
        """The links in root's tree, and in the declarative shadow roots it hosts."""
        shadow_roots: dict[int, LexborNode] | None = None
        for node in root.css(self._selector):
            tag = node.tag
            if tag == "template":
                if shadow_roots is None:  # finding templates costs a pass over the tree
                    shadow_roots = _shadow_roots(root)
                if (shadow_root := shadow_roots.get(node.mem_id)) is not None:
                    self.extract(shadow_root)
            elif tag in _HYPERLINKS:
                self._hyperlink(node)
            elif tag in _FRAMES:
                if src := node.attrs.get("src"):  # an empty src loads about:blank
                    self._add(src, True)
            elif tag == "link":
                self._link(node)
            elif tag == "meta":
                self._refresh(node.attrs.get("content") or "")
            else:
                self._other(node)

    def _hyperlink(self, node: LexborNode) -> None:
        attributes = node.attrs  # a view: cheaper than node.attributes, which copies them all
        # A valueless href is "", a link to the page itself. [href] also matches an SVG <a>
        # that has only xlink:href.
        if (href := attributes.get("href")) is not None or "href" in attributes:
            self._add(href or "", True)
        elif "xlink:href" in attributes:
            self._add(attributes.get("xlink:href") or "", True)
        if self._all_urls and (ping := attributes.get("ping")):
            for url in _SPLIT_ASCII_WHITESPACE(ping):
                if url:
                    self._add(url)

    def _link(self, node: LexborNode) -> None:
        attributes = node.attrs
        if "href" in attributes:
            href = attributes.get("href") or ""
            rel = (attributes.get("rel") or "").translate(_ASCII_LOWERCASE)
            tokens = set(_SPLIT_ASCII_WHITESPACE(rel))
            if "stylesheet" in tokens:  # an alternative style sheet isn't a hyperlink
                tokens.discard("alternate")
            if not tokens.isdisjoint(_HYPERLINK_RELS):
                self._add(href, True)
            elif self._all_urls and href:  # a resource such as a style sheet or an icon
                self._add(href)
        if self._all_urls and (srcset := attributes.get("imagesrcset")):
            for url in parse_srcset(srcset):
                self._add(url)

    def _refresh(self, content: str) -> None:
        """The HTML Standard's declarative refresh: only the first that works counts."""
        if self._refreshed or not content:
            return
        valid, url = parse_refresh(content)
        if not valid:
            return
        if url is not None:  # otherwise the page refreshes itself: not a link
            try:
                ada_url.join_url(self._base_url, url)
            except ValueError:  # a refresh to an invalid URL doesn't happen
                return
            self._add(url, True)
        self._refreshed = True

    def _other(self, node: LexborNode) -> None:
        """--all-urls: a resource, form or citation's URLs."""
        attributes = node.attrs
        for name in _URL_ATTRIBUTES[node.tag or ""]:
            value = attributes.get(name)
            if not value:
                if name in _ACTION and name in attributes:
                    self._add(self._document_url)  # submits to the page's own URL
                elif name == "cite" and name in attributes:
                    self._add("")
            elif name in _SRCSET:
                for url in parse_srcset(value):
                    self._add(url)
            else:
                self._add(value)

    def _add(self, value: str, navigational: bool = False) -> None:
        try:
            url = _join_url(self._base_url, value)
        except ValueError:  # not a URL a browser could follow
            return
        if not (url.startswith(_HTTP) or self._all_urls):
            return
        if (hash_at := url.find("#")) != -1:  # urls.strip_fragment, inlined
            url = url[:hash_at]
        self.printed[url] = None
        if navigational and url.startswith(self._scope):
            # Both /~a and /%7Ea are printed, as written, but they're one resource to crawl.
            # dedupe_key returns url itself without a "%", as most URLs are.
            self.followed.setdefault(dedupe_key(url) if "%" in url else url, url)


def _shadow_roots(root: LexborHTMLParser | LexborNode) -> dict[int, LexborNode]:
    """root's declarative shadow roots, by the template that declares each.

    lexbor keeps <template shadowrootmode> as a template; a browser attaches its content to
    the parent element as a shadow root. Only modes "open" and "closed" do that, only on a
    valid shadow host, and only once per host: a later template of the same mode replaces
    the root's content, and one of the other mode stays an ordinary, inert template.
    """
    shadow_roots: dict[int, LexborNode] = {}
    attached: dict[int, tuple[str, int]] = {}  # host: its root's mode, and its template
    for fragment in root.template_fragments():
        template = fragment.parent
        host = template.parent if template is not None else None
        if template is None or host is None or not _can_host(host.tag):
            continue
        mode = (template.attrs.get("shadowrootmode") or "").translate(_ASCII_LOWERCASE)
        if mode not in ("open", "closed"):
            continue
        current = attached.get(host.mem_id)
        if current is not None:
            if current[0] != mode:
                continue
            del shadow_roots[current[1]]
        attached[host.mem_id] = (mode, template.mem_id)
        shadow_roots[template.mem_id] = fragment
    return shadow_roots


def _can_host(tag: str | None) -> bool:
    """DOM's "valid shadow host name"."""
    if tag is None:
        return False
    return tag in _SHADOW_HOSTS or (
        _CUSTOM_ELEMENT_NAME(tag) is not None and tag not in _RESERVED_NAMES
    )


def parse_refresh(content: str) -> tuple[bool, str | None]:
    """A <meta http-equiv=refresh> content attribute, by the HTML Standard's shared
    declarative refresh steps: whether it refreshes at all, and the URL it names, if any.
    "5", "5; url=/next" and "0;'/next'" all refresh; "-1" and "5x" don't."""
    position = _skip(content, 0, _ASCII_WHITESPACE)
    start = position
    position = _skip(content, position, "0123456789")
    if position == start and not content.startswith(".", position):
        return False, None
    position = _skip(content, position, "0123456789.")  # a fraction, which is ignored
    if position < len(content):
        if content[position] not in ";," + _ASCII_WHITESPACE:
            return False, None
        position = _skip(content, position, _ASCII_WHITESPACE)
        if content.startswith((";", ","), position):
            position += 1
        position = _skip(content, position, _ASCII_WHITESPACE)
    if position == len(content):
        return True, None
    url = content[position:]
    if content[position] in "Uu":
        # "URL=", in any case and with spaces around "=". Without the "=", the whole of the
        # rest is the URL.
        after = position + 1
        if content[after : after + 2].translate(_ASCII_LOWERCASE) != "rl":
            return True, url
        after = _skip(content, after + 2, _ASCII_WHITESPACE)
        if not content.startswith("=", after):
            return True, url
        position = _skip(content, after + 1, _ASCII_WHITESPACE)
    if content[position : position + 1] in ("'", '"'):
        quote = content[position]
        url = content[position + 1 :]
        if (end := url.find(quote)) != -1:
            url = url[:end]
    else:
        url = content[position:]
    return True, url


def parse_srcset(srcset: str) -> list[str]:
    """The URLs of a srcset attribute's image candidates, by the HTML Standard's "parse a
    srcset attribute". A candidate whose descriptors are invalid is dropped, as a browser
    drops it: "a.png 1x, b.png 2x" lists both, "a.png 2y" neither."""
    urls: list[str] = []
    position = 0
    while True:
        position = _skip(srcset, position, _ASCII_WHITESPACE + ",")
        if position == len(srcset):
            return urls
        end = position
        while end < len(srcset) and srcset[end] not in _ASCII_WHITESPACE:
            end += 1
        url = srcset[position:end]
        position = end
        descriptors: list[str] = []
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            descriptors, position = _srcset_descriptors(srcset, position)
        if _valid_descriptors(descriptors):
            urls.append(url)


def _srcset_descriptors(srcset: str, position: int) -> tuple[list[str], int]:
    """The descriptor tokenizer of "parse a srcset attribute", from position: a candidate's
    descriptors, and the position after them."""
    position = _skip(srcset, position, _ASCII_WHITESPACE)
    descriptors: list[str] = []
    current = ""
    in_parens = False
    while position < len(srcset):
        char = srcset[position]
        if in_parens:
            current += char
            in_parens = char != ")"
        elif char in _ASCII_WHITESPACE:
            if current:
                descriptors.append(current)
                current = ""
        elif char == ",":
            position += 1
            break
        else:
            current += char
            in_parens = char == "("
        position += 1
    if current:
        descriptors.append(current)
    return descriptors, position


_NON_NEGATIVE_INTEGER = re.compile("[0-9]+").fullmatch
_FLOAT = re.compile(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?").fullmatch


def _valid_descriptors(descriptors: list[str]) -> bool:
    """Whether a srcset candidate's descriptors are valid: at most one of a width ("100w")
    and a density ("2x"), and a height ("50h") only with a width."""
    width = density = height = False
    for descriptor in descriptors:
        number, kind = descriptor[:-1], descriptor[-1:]
        if kind == "w" and _NON_NEGATIVE_INTEGER(number) and not (width or density):
            width = int(number) > 0
            if not width:
                return False
        elif kind == "x" and _FLOAT(number) and not (width or density or height):
            density = True
            if float(number) < 0:
                return False
        elif kind == "h" and _NON_NEGATIVE_INTEGER(number) and not (height or density):
            height = int(number) > 0
            if not height:
                return False
        else:
            return False
    return width or not height


def _skip(text: str, position: int, chars: str) -> int:
    """The position of the first character at or after position that isn't in chars."""
    while position < len(text) and text[position] in chars:
        position += 1
    return position


def parse_html(
    body: bytes, http_charset: str | None, *, truncated: bool = False
) -> tuple[LexborHTMLParser, Charset]:
    """The page's tree, decoded as a browser decodes it, and the charset that decoded it.

    Scripting is off, so <noscript> is parsed as markup. A charset that a <meta> or a guess
    gave is tentative: a browser's parser that reaches a <meta> declaring another encoding
    starts over in that one, which is how a declaration beyond the first 1,024 bytes counts.
    """
    charset = pick_charset(body, http_charset, truncated=truncated)
    tree = LexborHTMLParser(_markup(body, charset.encoding))
    if charset.tentative and (declared := _declared_encoding(tree)) is not None:
        encoding = change_encoding(charset.encoding, declared)
        if encoding is not None:
            tree = LexborHTMLParser(_markup(body, encoding))
        charset = Charset(encoding or charset.encoding, Source.META)
    return tree, charset


def _markup(body: bytes, encoding: str) -> bytes | str:
    """What lexbor parses: UTF-8 as bytes, which it decodes itself without a copy, and
    anything else as text."""
    if encoding == UTF_8:
        # lexbor would read a byte order mark as text, which ends <head> early and makes a
        # <frameset> after it be ignored.
        return body[len(_UTF_8_BOM) :] if body.startswith(_UTF_8_BOM) else body
    return decode(body, encoding)


def _declared_encoding(tree: LexborHTMLParser) -> str | None:
    """The encoding the first <meta> that declares one declares: the one a parser acts on."""
    first = tree.css_first(_DECLARING_META)
    if first is None:
        return None
    if (encoding := _meta_encoding(first)) is not None:
        return encoding
    # Rarely, the first declares nothing it can use, such as charset="utf-7".
    return next(filter(None, map(_meta_encoding, tree.css(_DECLARING_META)[1:])), None)


def _meta_encoding(meta: LexborNode) -> str | None:
    attributes = meta.attrs
    return meta_encoding(
        attributes.get("charset"), attributes.get("http-equiv"), attributes.get("content")
    )
