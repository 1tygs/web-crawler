"""A page in, its output record and in-scope links out (DESIGN.md §5.5).

parse_page is a pure function: bytes in, bytes and strings out, no I/O and no shared state.
That's what lets the same code run inline on the event loop or in a pool of processes.
"""

import ada_url
from selectolax.lexbor import LexborHTMLParser, LexborNode

from web_crawler.content import Charset, Source, change_encoding, meta_encoding, pick_charset
from web_crawler.encodings import UTF_8, decode
from web_crawler.types import ParseJob, ParseResult, WorkerContext
from web_crawler.urls import dedupe_key, strip_fragment

# Navigational links (DESIGN.md §2.4) as one selector list, which lexbor matches in document
# order.
_LINKS = "a[href], area[href], iframe[src], frame[src]"
_SRC_TAGS = frozenset({"iframe", "frame"})
_HTTP = ("http:", "https:")
_DECLARING_META = "meta[charset], meta[http-equiv][content]"
_UTF_8_BOM = b"\xef\xbb\xbf"


def parse_page(job: ParseJob, ctx: WorkerContext) -> ParseResult:
    """Every link on the page, resolved as a browser resolves it, in document order."""
    tree, _ = parse_html(job.body, job.http_charset, truncated=job.truncated)
    printed: list[str] = []
    seen: set[str] = set()
    for node in tree.css(_LINKS):
        value = node.attributes.get("src" if node.tag in _SRC_TAGS else "href")
        if value is None:
            # A valueless attribute, or an SVG <a> that has only xlink:href, which [href]
            # also matches. Both are skipped rather than read as "" (a link to this page).
            continue
        try:
            url = ada_url.join_url(job.url, value)
        except ValueError:  # not a URL a browser could follow
            continue
        if not url.startswith(_HTTP):
            continue
        url = strip_fragment(url)
        if url not in seen:
            seen.add(url)
            printed.append(url)
    # Both /~a and /%7Ea are printed, as written, but they're one resource to crawl.
    links: list[tuple[str, str]] = []
    keys: set[str] = set()
    for url in printed:
        if url.startswith(ctx.scope_prefixes) and (key := dedupe_key(url)) not in keys:
            keys.add(key)
            links.append((key, url))
    record = "".join([job.url, "\n", *[f"  {url}\n" for url in printed], "\n"]).encode()
    return ParseResult(record=record, links=links, links_found=len(printed))


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
    attributes = meta.attributes
    return meta_encoding(
        attributes.get("charset"), attributes.get("http-equiv"), attributes.get("content")
    )
