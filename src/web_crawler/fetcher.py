"""One item's fetch: redirects, triage and the body (DESIGN.md §5.2).

The fetcher follows redirects itself, so each hop is checked against the scope before it's
requested, and reads only the bodies worth reading. What to do with a response is decided
by triage() from the status and headers alone.
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass

import ada_url

from web_crawler.content import HTML_TYPES, parse_content_type
from web_crawler.stats import Stats
from web_crawler.transport import Response, Transport, TransportError
from web_crawler.types import CrawlItem, ParseJob
from web_crawler.urls import Scope, strip_fragment

MAX_REDIRECTS = 10
# A response that isn't wanted is read to the end, so that its connection can be reused,
# only when it's this small. A bigger one is cheaper to drop with its connection.
DRAIN_LIMIT = 64 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SUCCESSFUL = range(200, 300)


@dataclass(frozen=True, slots=True)
class Page:
    """An HTML page, ready to parse."""

    job: ParseJob


@dataclass(frozen=True, slots=True)
class NotPage:
    """A 2xx response that isn't HTML: counted, never parsed."""

    url: str
    content_type: str | None


@dataclass(frozen=True, slots=True)
class Final:
    """The item ended without a page."""

    reason: str  # short and stable, for the summary's counts: "HTTP 404", "connect", ...
    detail: str  # what happened, for messages
    url: str  # the URL whose response, or failure to respond, ended it


type FetchOutcome = Page | NotPage | Final


class Triage(enum.Enum):
    REDIRECT = enum.auto()
    PAGE = enum.auto()
    NOT_PAGE = enum.auto()
    FINAL = enum.auto()


def triage(status: int, headers: Mapping[str, str]) -> Triage:
    """What to do with a response, from its status and headers. A page is a 2xx response
    whose Content-Type is HTML (DESIGN.md §2.4)."""
    if status in REDIRECT_STATUSES and "Location" in headers:
        return Triage.REDIRECT
    if status in SUCCESSFUL:
        essence, _ = parse_content_type(headers.get("Content-Type"))
        return Triage.PAGE if essence in HTML_TYPES else Triage.NOT_PAGE
    return Triage.FINAL


class Fetcher:
    def __init__(self, transport: Transport, scope: Scope, max_page_bytes: int, stats: Stats):
        self._transport = transport
        self._scope = scope
        self._max_page_bytes = max_page_bytes
        self._stats = stats

    async def fetch(self, item: CrawlItem) -> FetchOutcome:
        """Request item.url and follow its redirects to a page, or to why there isn't one."""
        url = previous = item.url
        for _ in range(1 + MAX_REDIRECTS):
            try:
                async with self._transport.open(url) as response:
                    outcome = await self._handle(response)
            except TransportError as error:
                return Final(str(error.kind), str(error), url)
            if not isinstance(outcome, str):
                return outcome
            url, previous = outcome, url
        detail = f"more than {MAX_REDIRECTS} redirects; the last was to {url}"
        return Final("too many redirects", detail, previous)

    async def _handle(self, response: Response) -> FetchOutcome | str:
        """The outcome of one response, or the URL it redirects to."""
        status = response.status
        self._stats.statuses[status] += 1
        match triage(status, response.headers):
            case Triage.REDIRECT:
                location = response.headers["Location"]
                try:
                    target = strip_fragment(ada_url.join_url(response.url, location))
                except ValueError:
                    detail = f"HTTP {status} to {location!r}, which isn't a valid URL"
                    return Final("bad redirect", detail, response.url)
                if not self._scope.contains(target):
                    detail = f"HTTP {status} to {target}, outside the crawl's scope"
                    return Final("redirect out of scope", detail, response.url)
                await response.read(DRAIN_LIMIT)
                return target
            case Triage.PAGE:
                body, _truncated = await response.read(self._max_page_bytes)
                return Page(ParseJob(url=response.url, body=body))
            case Triage.NOT_PAGE:
                size = _content_length(response.headers)
                if size is not None and size <= DRAIN_LIMIT:
                    await response.read(DRAIN_LIMIT)
                return NotPage(response.url, response.headers.get("Content-Type"))
            case Triage.FINAL:  # pragma: no branch  (the cases are exhaustive)
                return Final(f"HTTP {status}", f"HTTP {status}", response.url)


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("Content-Length", "").strip()
    return int(value) if value.isdigit() and value.isascii() else None
