"""A page in, its output record and in-scope links out (DESIGN.md §5.5).

parse_page is a pure function: bytes in, bytes and strings out, no I/O and no shared state.
That's what lets the same code run inline on the event loop or in a pool of processes.
"""

import ada_url
from selectolax.lexbor import LexborHTMLParser

from web_crawler.types import ParseJob, ParseResult, WorkerContext
from web_crawler.urls import strip_fragment

# Navigational links (DESIGN.md §2.4) as one selector list, which lexbor matches in document
# order.
_LINKS = "a[href], area[href], iframe[src], frame[src]"
_SRC_TAGS = frozenset({"iframe", "frame"})
_HTTP = ("http:", "https:")


def parse_page(job: ParseJob, ctx: WorkerContext) -> ParseResult:
    """Every link on the page, resolved as a browser resolves it, in document order."""
    tree = LexborHTMLParser(job.body)  # UTF-8; scripting off, so <noscript> is markup
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
    # The dedupe key is the display URL itself. URLs that differ only in percent-encoding,
    # such as %7E and ~, stay distinct until dedupe_key (DESIGN.md §5.6) merges them.
    links = [(url, url) for url in printed if url.startswith(ctx.scope_prefixes)]
    record = "".join([job.url, "\n", *[f"  {url}\n" for url in printed], "\n"]).encode()
    return ParseResult(record=record, links=links, links_found=len(printed))
