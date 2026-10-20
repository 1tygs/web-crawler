"""One item's fetch: redirects, triage and the body (DESIGN.md §5.2); and robots.txt
(DESIGN.md §5.7).

The fetcher follows redirects itself, so that each hop is checked and claimed before it's
requested, and reads only the bodies worth reading. What to do with a response is decided
by triage() from the status and headers alone, and whether a failure is worth another try
by retry.classify().
"""

import asyncio
import contextlib
import dataclasses
import enum
import random
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import ada_url

from web_crawler import robots
from web_crawler.content import (
    HTML_TYPES,
    SNIFF_BYTES,
    UNDECODABLE,
    UNKNOWN_TYPES,
    ContentDecodeError,
    decode_body,
    nosniff,
    parse_content_type,
    sniff_html,
)
from web_crawler.frontier import Admission, Hop
from web_crawler.limiter import parse_retry_after
from web_crawler.retry import Class, classify
from web_crawler.robots import Availability, RobotsTxt
from web_crawler.stats import Stats
from web_crawler.transport import Response, Transport, TransportError
from web_crawler.types import CrawlItem, ErrorKind, ParseJob
from web_crawler.urls import dedupe_key, strip_fragment

MAX_REDIRECTS = 10
# A response that isn't wanted is read to the end, so that its connection can be reused,
# only when it's this small. A bigger one is cheaper to drop with its connection.
DRAIN_LIMIT = 64 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SUCCESSFUL = range(200, 300)
# How much of a body without a usable type is read to sniff it: far more than its first
# SNIFF_BYTES need, even compressed.
SNIFF_READ = 16 * 1024


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


@dataclass(frozen=True, slots=True)
class Retry:
    """The item failed in a way that another try may not (DESIGN.md §5.4)."""

    item: CrawlItem  # to request again: the item where it failed, as resume() makes it
    cls: Class  # what kind of failure, which says what budget a retry uses
    final: Final  # how the item ends if it isn't tried again
    retry_after: float | None = None  # seconds a 429 or 503's Retry-After asks for


type FetchOutcome = Page | NotPage | Claimed | Retry | Final


def resume(item: CrawlItem, chain: Sequence[str]) -> CrawlItem:
    """item, to request again where it got to: the last of chain, the URLs it requested,
    after the redirects that led there. Starting again from its first URL would find the
    redirects' targets claimed, by this item, and nothing would print the page."""
    if len(chain) == len(item.redirected_from) + 1:
        return item
    url = chain[-1]
    return dataclasses.replace(
        item, url=url, key=dedupe_key(url), redirected_from=tuple(chain[:-1])
    )


class HopPolicy(Protocol):
    """Decides whether an item follows a redirect. It's async so that the start URL's can
    fetch robots.txt for an origin a redirect leads to (DESIGN.md §2.4)."""

    async def admit_hop(self, chain: Collection[str], url: str, key: str) -> Hop:
        """Whether to request url, whose dedupe key is key; chain holds the keys of the URLs
        requested so far."""
        ...


class Signals(Protocol):
    """What each request says about the server's load: the concurrency limiter's inputs
    (DESIGN.md §5.3). Times are the fetcher's clock's."""

    def answered(self, status: int, ttfb: float, issued_at: float) -> None:
        """The headers of a request sent at issued_at arrived, ttfb seconds after it started."""
        ...

    def failed(self, kind: ErrorKind, issued_at: float) -> None:
        """A request sent at issued_at got no response, or its body was cut off."""
        ...


class _Unheard:
    """Signals for no one: a fetcher that has no limiter to feed."""

    def answered(self, status: int, ttfb: float, issued_at: float) -> None:
        pass

    def failed(self, kind: ErrorKind, issued_at: float) -> None:
        pass


class AdmissionHops:
    """The hop policy of every item but the start URL: admission decides, at once."""

    def __init__(self, admission: Admission) -> None:
        self._admission = admission

    async def admit_hop(self, chain: Collection[str], url: str, key: str) -> Hop:
        return self._admission.admit_hop(chain, url, key)


class Triage(enum.Enum):
    REDIRECT = enum.auto()
    PAGE = enum.auto()
    SNIFF = enum.auto()  # a page if its first bytes are HTML
    NOT_PAGE = enum.auto()
    FINAL = enum.auto()


def triage(status: int, headers: Mapping[str, str]) -> Triage:
    """What to do with a response, from its status and headers. A page is a 2xx response
    whose Content-Type is HTML, or that has no usable type and whose body is sniffed as HTML,
    unless the server forbids sniffing (DESIGN.md §2.4, MIME Sniffing)."""
    if status in REDIRECT_STATUSES and "Location" in headers:
        return Triage.REDIRECT
    if status in SUCCESSFUL:
        essence, _ = parse_content_type(headers.get("Content-Type"))
        if essence in HTML_TYPES:
            return Triage.PAGE
        if essence is None or essence in UNKNOWN_TYPES:
            forbidden = nosniff(headers.get("X-Content-Type-Options"))
            return Triage.NOT_PAGE if forbidden else Triage.SNIFF
        return Triage.NOT_PAGE
    return Triage.FINAL


@dataclass(frozen=True, slots=True)
class _Redirect:
    status: int
    target: str  # resolved, without its fragment


# How an item ends at a redirect that it may not follow: the reason, and why.
_STOPPED = {
    Hop.OUT_OF_SCOPE: ("redirect out of scope", "outside the crawl's scope"),
    Hop.DISALLOWED: ("redirect disallowed", "which robots.txt disallows"),
    Hop.TRAP: ("redirect to a trap", "which a trap rule rejects"),
}


class Fetcher:
    def __init__(  # noqa: PLR0913  (the collaborators that tests replace)
        self,
        transport: Transport,
        max_page_bytes: int,
        stats: Stats,
        *,
        signals: Signals | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._max_page_bytes = max_page_bytes
        self._stats = stats
        self._signals = signals or _Unheard()
        self._clock = clock
        self._wall_clock = wall_clock  # for Retry-After's dates

    async def fetch(self, item: CrawlItem, hops: HopPolicy) -> FetchOutcome:
        """Request item.url and follow its redirects to a page, or to why there isn't one.

        Each redirect target is claimed before it's requested (hops), so a page reached by
        redirect is fetched once, however many URLs lead to it. A target that comes back
        within the chain is followed again, as a cookie gate needs; a real loop ends after
        MAX_REDIRECTS redirects, and is reported as one. An item tried again carries the
        redirects it followed before, and the chain goes on from them.

        A failure that another try may not repeat is a Retry; whether there is one is the
        caller's to decide.
        """
        chain = [*item.redirected_from, item.url]
        keys = {dedupe_key(url) for url in item.redirected_from}
        keys.add(item.key)
        while True:
            issued = self._clock()
            try:
                async with self._transport.open(chain[-1]) as response:
                    self._signals.answered(response.status, response.ttfb, issued)
                    outcome = await self._handle(response, tuple(chain), item)
            except TransportError as error:
                self._signals.failed(error.kind, issued)
                final = Final(str(error.kind), str(error), tuple(chain))
                return _retry_or(final, classify(error.kind), item)
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

    async def probe(self, url: str) -> int | None:
        """The status of one request for url, with no redirect followed and no body read,
        or None if it got no response: a check on a host the crawl paused for, which the
        concurrency limit doesn't count (DESIGN.md §5.3)."""
        try:
            async with self._transport.open(url) as response:
                return response.status
        except TransportError:
            return None

    async def _handle(
        self, response: Response, chain: tuple[str, ...], item: CrawlItem
    ) -> FetchOutcome | _Redirect:
        """The outcome of item's request for the last URL of chain, or the redirect it
        makes."""
        status = response.status
        self._stats.statuses[status] += 1
        match triage(status, response.headers):
            case Triage.REDIRECT:
                await _drain(response)
                location = response.headers["Location"]
                try:
                    target = strip_fragment(ada_url.join_url(response.url, location))
                except ValueError:
                    detail = f"HTTP {status} to {location!r}, which isn't a valid URL"
                    return Final("bad redirect", detail, chain, status)
                return _Redirect(status, target)
            case Triage.PAGE:
                body, truncated = await response.read(self._max_page_bytes)
                return _page(response, body, truncated)
            case Triage.SNIFF:
                return await self._sniff(response, chain)
            case Triage.NOT_PAGE:
                await _drain_if_small(response)
                return NotPage(response.url, response.headers.get("Content-Type"))
            case Triage.FINAL:  # pragma: no branch  (the cases are exhaustive)
                return await self._failed(response, chain, item)

    async def _failed(
        self, response: Response, chain: tuple[str, ...], item: CrawlItem
    ) -> Retry | Final:
        """An answer that isn't a page or a redirect: final, or worth another try. A 429 or
        503 says how long to wait in its Retry-After, if it says."""
        await _drain_if_small(response)
        status = response.status
        final = Final(f"HTTP {status}", f"HTTP {status}", chain, status)
        if (cls := classify(status)) is not Class.THROTTLE:
            return _retry_or(final, cls, item)
        value = response.headers.get("Retry-After")
        return Retry(resume(item, chain), cls, final, parse_retry_after(value, self._wall_clock()))

    async def _sniff(self, response: Response, chain: tuple[str, ...]) -> Page | NotPage | Final:
        """A 2xx response without a usable type: a page if its body starts as HTML does. The
        body is still compressed, so the start of it is decompressed to sniff."""
        start, more = await response.read(min(SNIFF_READ, self._max_page_bytes))
        try:
            header, _ = decode_body(start, response.headers.get("Content-Encoding"), SNIFF_BYTES)
        except ContentDecodeError as error:
            return Final(UNDECODABLE, f"HTTP {response.status}, {error}", chain, response.status)
        if not sniff_html(header):
            if more:
                await _drain_if_small(response)
            return NotPage(response.url, response.headers.get("Content-Type"))
        rest, truncated = b"", False
        if more:
            rest, truncated = await response.read(self._max_page_bytes - len(start))
        return _page(response, start + rest, truncated)


@dataclass(frozen=True, slots=True)
class _Answer:
    """One response to a robots.txt request, as much of it as the file's fate needs."""

    status: int
    body: bytes = b""  # decompressed
    truncated: bool = False
    location: str | None = None  # for a redirect: where to, resolved; "" if invalid
    undecodable: str | None = None  # what's wrong with a body that can't be decompressed


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
                if answer.undecodable is not None:  # what the rules are can't be known
                    detail = f"HTTP {answer.status}, {answer.undecodable}"
                    return RobotsTxt(url, Availability.UNREACHABLE, detail)
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
        retry, a 429, a 5xx, a body that couldn't be decompressed or an error, isn't kept for
        the next request."""
        if (pending := self._answers.get(url)) is not None:
            return await pending
        pending = self._answers[url] = asyncio.get_running_loop().create_future()
        try:
            answer = await self._request(url)
        except BaseException:
            del self._answers[url]
            pending.cancel()  # whoever waits on it stops too
            raise
        if (
            isinstance(answer, TransportError)
            or robots.unreachable(answer.status)
            or answer.undecodable is not None
        ):
            del self._answers[url]
        pending.set_result(answer)
        return answer

    async def _request(self, url: str) -> _Answer | TransportError:
        try:
            async with self._transport.open(url) as response:
                status = response.status
                if status in SUCCESSFUL:
                    # RFC 9309's limit is on the file decompressed. As many compressed bytes
                    # hold at least that much of any file that compresses at all.
                    body, more = await response.read(robots.MAX_BYTES)
                    encoding = response.headers.get("Content-Encoding")
                    try:
                        text, cut = decode_body(body, encoding, robots.MAX_BYTES)
                    except ContentDecodeError as error:
                        return _Answer(status, undecodable=str(error))
                    return _Answer(status, text, more or cut)
                await _drain(response)
                if status in REDIRECT_STATUSES and "Location" in response.headers:
                    try:
                        location = ada_url.join_url(url, response.headers["Location"])
                    except ValueError:
                        location = ""
                    return _Answer(status, location=strip_fragment(location))
                return _Answer(status)
        except TransportError as error:
            return error


def _retry_or(final: Final, cls: Class, item: CrawlItem) -> Retry | Final:
    """A Retry of item where final ended it, if final is worth another try."""
    return final if cls is Class.FINAL else Retry(resume(item, final.chain), cls, final)


def _too_many_redirects(chain: tuple[str, ...], looped: bool, target: str, status: int) -> Final:
    if looped:  # some URL was requested twice
        detail = f"redirects in a loop through {', '.join(dict.fromkeys(chain))}"
        return Final("redirect loop", detail, chain, status)
    detail = f"more than {MAX_REDIRECTS} redirects; the last was to {target}"
    return Final("too many redirects", detail, chain, status)


def _page(response: Response, body: bytes, truncated: bool) -> Page:
    """The page in response, whose body, still compressed, has been read."""
    headers = response.headers
    _, charset = parse_content_type(headers.get("Content-Type"))
    encoding = headers.get("Content-Encoding")
    return Page(ParseJob(response.url, body, charset, truncated, encoding))


async def _drain_if_small(response: Response) -> None:
    """Read the rest of an unwanted body if it says it's small, so that its connection can
    be reused; a bigger one, or one of unknown size, is cheaper to drop with its
    connection."""
    size = _content_length(response.headers)
    if size is not None and size <= DRAIN_LIMIT:
        await _drain(response)


async def _drain(response: Response) -> None:
    """Read up to DRAIN_LIMIT more bytes of an unwanted body, so that its connection can be
    reused if that was the rest of it. One that fails to arrive is no matter: its
    connection is closed instead."""
    with contextlib.suppress(TransportError):
        await response.read(DRAIN_LIMIT)


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = headers.get("Content-Length", "").strip()
    return int(value) if value.isdigit() and value.isascii() else None
