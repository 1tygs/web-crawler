"""One item's fetch: redirects, triage and the body (DESIGN.md §5.2); and robots.txt
(DESIGN.md §5.7).

The fetcher follows redirects itself, so that each hop is checked and claimed before it's
requested, and reads only the bodies worth reading. What to do with a response is decided
by triage() from the status and headers alone.
"""

import asyncio
import enum
import random
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import ada_url

from web_crawler import robots
from web_crawler.content import HTML_TYPES, parse_content_type
from web_crawler.frontier import Admission, Hop
from web_crawler.robots import Availability, RobotsTxt
from web_crawler.stats import Stats
from web_crawler.transport import Response, Transport, TransportError
from web_crawler.types import CrawlItem, ParseJob
from web_crawler.urls import dedupe_key, strip_fragment

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
class Claimed:
    """The item redirected to a URL that another item claimed, and prints."""

    url: str  # where the redirect led
    chain: tuple[str, ...]  # the URLs the item requested, in order


@dataclass(frozen=True, slots=True)
class Final:
    """The item ended without a page."""

    reason: str  # short and stable, for the summary's counts: "HTTP 404", "connect", ...
    detail: str  # what happened, for messages
    chain: tuple[str, ...]  # the URLs requested, in order; the last one's answer ended it
    status: int | None = None  # that answer's status, if it was an HTTP response
    location: str | None = None  # where a redirect that wasn't followed led

    @property
    def url(self) -> str:
        return self.chain[-1]


type FetchOutcome = Page | NotPage | Claimed | Final


class HopPolicy(Protocol):
    """Decides whether an item follows a redirect. It's async so that the start URL's can
    fetch robots.txt for an origin a redirect leads to (DESIGN.md §2.4)."""

    async def admit_hop(self, chain: Collection[str], url: str, key: str) -> Hop:
        """Whether to request url, whose dedupe key is key; chain holds the keys of the URLs
        requested so far."""
        ...


class AdmissionHops:
    """The hop policy of every item but the start URL: admission decides, at once."""

    def __init__(self, admission: Admission) -> None:
        self._admission = admission

    async def admit_hop(self, chain: Collection[str], url: str, key: str) -> Hop:
        return self._admission.admit_hop(chain, url, key)


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


@dataclass(frozen=True, slots=True)
class _Redirect:
    status: int
    target: str  # resolved, without its fragment


# How an item ends at a redirect that it may not follow: the reason, and why.
_STOPPED = {
    Hop.OUT_OF_SCOPE: ("redirect out of scope", "outside the crawl's scope"),
    Hop.DISALLOWED: ("redirect disallowed", "which robots.txt disallows"),
}


class Fetcher:
    def __init__(self, transport: Transport, max_page_bytes: int, stats: Stats) -> None:
        self._transport = transport
        self._max_page_bytes = max_page_bytes
        self._stats = stats

    async def fetch(self, item: CrawlItem, hops: HopPolicy) -> FetchOutcome:
        """Request item.url and follow its redirects to a page, or to why there isn't one.

        Each redirect target is claimed before it's requested (hops), so a page reached by
        redirect is fetched once, however many URLs lead to it. A target that comes back
        within the chain is followed again, as a cookie gate needs; a real loop ends after
        MAX_REDIRECTS redirects, and is reported as one.
        """
        chain = [item.url]
        keys = {item.key}
        while True:
            try:
                async with self._transport.open(chain[-1]) as response:
                    outcome = await self._handle(response, tuple(chain))
            except TransportError as error:
                return Final(str(error.kind), str(error), tuple(chain))
            if not isinstance(outcome, _Redirect):
                return outcome
            target, status = outcome.target, outcome.status
            if len(chain) > MAX_REDIRECTS:
                return _too_many_redirects(tuple(chain), len(keys) < len(chain), target, status)
            # The response is released by now: a hop policy may wait on other requests.
            hop = await hops.admit_hop(keys, target, key := dedupe_key(target))
            if hop is Hop.FOLLOW:
                chain.append(target)
                keys.add(key)
            elif hop is Hop.CLAIMED:
                return Claimed(target, tuple(chain))
            else:
                reason, why = _STOPPED[hop]
                detail = f"HTTP {status} to {target}, {why}"
                return Final(reason, detail, tuple(chain), status, target)

    async def _handle(self, response: Response, chain: tuple[str, ...]) -> FetchOutcome | _Redirect:
        """The outcome of one response, the last of chain, or the redirect it makes."""
        status = response.status
        self._stats.statuses[status] += 1
        match triage(status, response.headers):
            case Triage.REDIRECT:
                await response.read(DRAIN_LIMIT)  # so its connection can be reused
                location = response.headers["Location"]
                try:
                    target = strip_fragment(ada_url.join_url(response.url, location))
                except ValueError:
                    detail = f"HTTP {status} to {location!r}, which isn't a valid URL"
                    return Final("bad redirect", detail, chain, status)
                return _Redirect(status, target)
            case Triage.PAGE:
                body, truncated = await response.read(self._max_page_bytes)
                _, charset = parse_content_type(response.headers.get("Content-Type"))
                return Page(ParseJob(response.url, body, charset, truncated))
            case Triage.NOT_PAGE:
                size = _content_length(response.headers)
                if size is not None and size <= DRAIN_LIMIT:
                    await response.read(DRAIN_LIMIT)
                return NotPage(response.url, response.headers.get("Content-Type"))
            case Triage.FINAL:  # pragma: no branch  (the cases are exhaustive)
                return Final(f"HTTP {status}", f"HTTP {status}", chain, status)


@dataclass(frozen=True, slots=True)
class _Answer:
    """One response to a robots.txt request, as much of it as the file's fate needs."""

    status: int
    body: bytes = b""
    truncated: bool = False
    location: str | None = None  # for a redirect: where to, resolved; "" if invalid


class RobotsFetcher:
    """Fetches each origin's robots.txt by RFC 9309 (DESIGN.md §5.7).

    It follows up to robots.MAX_REDIRECTS redirects, to other hosts too, and the file
    found governs the origin it was asked for. A 2xx answer is the file; 429, 5xx or no
    answer is tried robots.TRIES times, with backoff, before the file counts as unreachable;
    anything else, more redirects included, means there is no file. Within one round of
    fetch_all, each URL is requested once however many origins' chains lead to it, as the
    http origin's robots.txt often redirects to the https one's.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        rng: random.Random,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._rng = rng
        self._sleep = sleep
        self._answers: dict[str, asyncio.Future[_Answer | TransportError]] = {}

    async def fetch_all(self, origins: Sequence[str]) -> list[RobotsTxt]:
        """The robots.txt of each origin, fetched in parallel and afresh."""
        self._answers = {}
        return list(await asyncio.gather(*map(self._fetch, origins)))

    async def _fetch(self, origin: str) -> RobotsTxt:
        url = f"{origin}/robots.txt"
        for attempt in range(1, robots.TRIES + 1):
            file = await self._follow(url)
            if file.availability is not Availability.UNREACHABLE:
                return file
            if attempt < robots.TRIES:
                # Full jitter, as for pages (DESIGN.md §5.4): 0.5 s, then 1 s at most.
                await self._sleep(self._rng.uniform(0, 0.5 * 2 ** (attempt - 1)))
        detail = f"{file.detail}, {robots.TRIES} tries"
        return RobotsTxt(url, Availability.UNREACHABLE, detail, no_answer=file.no_answer)

    async def _follow(self, url: str) -> RobotsTxt:
        """The file at url, after its redirects."""
        target = url
        for _ in range(1 + robots.MAX_REDIRECTS):
            answer = await self._ask(target)
            if isinstance(answer, TransportError):
                kind = str(answer.kind)
                return RobotsTxt(url, Availability.UNREACHABLE, str(answer), no_answer=kind)
            if answer.location is None:
                if answer.status in SUCCESSFUL:
                    return RobotsTxt.found(url, answer.body, truncated=answer.truncated)
                return robots.without_file(url, answer.status)
            if not answer.location.startswith(("http://", "https://")):
                detail = f"HTTP {answer.status} to a URL that can't be fetched"
                return RobotsTxt(url, Availability.UNAVAILABLE, detail)
            target = answer.location
        detail = f"more than {robots.MAX_REDIRECTS} redirects"
        return RobotsTxt(url, Availability.UNAVAILABLE, detail)

    async def _ask(self, url: str) -> _Answer | TransportError:
        """url's answer, or what kept it from answering. An answer that could change on
        retry, a 429, a 5xx or an error, isn't kept for the next request."""
        if (pending := self._answers.get(url)) is not None:
            return await pending
        pending = self._answers[url] = asyncio.get_running_loop().create_future()
        try:
            answer = await self._request(url)
        except BaseException:
            del self._answers[url]
            pending.cancel()  # whoever waits on it stops too
            raise
        if isinstance(answer, TransportError) or robots.unreachable(answer.status):
            del self._answers[url]
        pending.set_result(answer)
        return answer

    async def _request(self, url: str) -> _Answer | TransportError:
        try:
            async with self._transport.open(url) as response:
                status = response.status
                if status in SUCCESSFUL:
                    body, truncated = await response.read(robots.MAX_BYTES)
                    return _Answer(status, body, truncated)
                await response.read(DRAIN_LIMIT)  # so its connection can be reused
                if status in REDIRECT_STATUSES and "Location" in response.headers:
                    try:
                        location = ada_url.join_url(url, response.headers["Location"])
                    except ValueError:
                        location = ""
                    return _Answer(status, location=strip_fragment(location))
                return _Answer(status)
        except TransportError as error:
            return error


def _too_many_redirects(chain: tuple[str, ...], looped: bool, target: str, status: int) -> Final:
    if looped:  # some URL was requested twice
        detail = f"redirects in a loop through {', '.join(dict.fromkeys(chain))}"
        return Final("redirect loop", detail, chain, status)
    detail = f"more than {MAX_REDIRECTS} redirects; the last was to {target}"
    return Final("too many redirects", detail, chain, status)


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("Content-Length", "").strip()
    return int(value) if value.isdigit() and value.isascii() else None
