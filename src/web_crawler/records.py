"""What the crawl prints (DESIGN.md §5.8): a record for each page, as text or as JSONL, and,
under --include-errors, a JSONL record for each URL that ended without a page.

Pure: the parsers render the pages' records, which the coordinator writes as they are, and
the coordinator renders the others.
"""

from collections.abc import Collection, Sequence

import orjson

from web_crawler.types import Format

FORMATS: tuple[Format, ...] = ("text", "jsonl")
_JSONL = orjson.OPT_APPEND_NEWLINE


def page_record(  # noqa: PLR0913  (a record's fields, most of them JSONL's alone)
    fmt: Format,
    url: str,
    links: Collection[str],
    *,
    status: int,
    depth: int,
    redirected_from: Sequence[str] = (),
    truncated: bool = False,
    canonical: str | None = None,
) -> bytes:
    """A page's record. As text: its URL, then each link indented by two spaces, then a
    blank line. As JSONL: an object with url, status, depth and links, then
    redirected_from, truncated and canonical when there's something to say."""
    if fmt == "text":
        return "".join([url, "\n", *[f"  {link}\n" for link in links], "\n"]).encode()
    fields: dict[str, object] = {"url": url, "status": status, "depth": depth}
    fields["links"] = list(links)
    if redirected_from:
        fields["redirected_from"] = redirected_from
    if truncated:
        fields["truncated"] = True
    if canonical is not None:
        fields["canonical"] = canonical
    return orjson.dumps(fields, option=_JSONL)


def error_record(  # noqa: PLR0913  (a record's fields, most of them for some errors alone)
    url: str,
    depth: int,
    error: str,
    *,
    status: int | None = None,
    detail: str | None = None,
    redirected_from: Sequence[str] = (),
    location: str | None = None,
    content_type: str | None = None,
) -> bytes:
    """The JSONL record of a URL that ended without a page: url, the last URL requested;
    status, its response's, if it had one; depth; and error, the reason the summary counts
    it under. Then, when there's something to say: detail, what happened, if error doesn't
    say it all; redirected_from; location, where a redirect that wasn't followed led; and
    content_type, a response's that isn't HTML."""
    fields: dict[str, object] = {"url": url}
    if status is not None:
        fields["status"] = status
    fields["depth"] = depth
    fields["error"] = error
    if detail is not None and detail != error:
        fields["detail"] = detail
    if redirected_from:
        fields["redirected_from"] = redirected_from
    if location is not None:
        fields["location"] = location
    if content_type is not None:
        fields["content_type"] = content_type
    return orjson.dumps(fields, option=_JSONL)
