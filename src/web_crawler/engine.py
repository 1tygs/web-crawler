"""The crawl: start-up, dispatch and termination (DESIGN.md §4.2, §4.3).

crawl() is the one place that builds concrete objects; everything else receives its
collaborators, so tests can swap in fakes. Start-up settles the start URL, its scope and
robots.txt before anything else is fetched. Then the dispatcher keeps a fixed number of
fetches in flight, and parses each page inline on the event loop. The first SIGINT or
SIGTERM stops it cleanly.
"""

import asyncio
import logging
import os
import random
import signal
import time
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, NoReturn

from web_crawler.config import Config
from web_crawler.fetcher import (
    AdmissionHops,
    Claimed,
    Fetcher,
    Final,
    HopPolicy,
    NotPage,
    Page,
    RobotsFetcher,
)
from web_crawler.frontier import Admission, Frontier, Hop
from web_crawler.output import Sink
from web_crawler.parse import parse_page
from web_crawler.robots import Availability, RobotsPolicy, RobotsTxt, product_token
from web_crawler.stats import Stats
from web_crawler.transport import AiohttpTransport, ErrorKind, Transport
from web_crawler.types import CrawlItem, ExitCode, WorkerContext
from web_crawler.urls import Scope, dedupe_key, normalize_start

log = logging.getLogger(__name__)

# The adaptive limiter's starting limit (DESIGN.md §5.3), held fixed until the limiter
# replaces it.
INITIAL_CONCURRENCY = 8

# Failures to reach a start URL given without a scheme after which http:// might work.
_HTTPS_FAILURES = frozenset({ErrorKind.CONNECT, ErrorKind.CONNECT_TIMEOUT, ErrorKind.TLS})
_ROBOTS_HINT = "if the site is yours, --ignore-robots skips robots.txt"

# The signals that stop a crawl cleanly, and the exit code each gives (DESIGN.md §5.9).
_STOP_SIGNALS = {signal.SIGINT: ExitCode.INTERRUPTED, signal.SIGTERM: ExitCode.TERMINATED}

type _SignalHandler = Callable[[int, FrameType | None], Any] | int | None


@dataclass(frozen=True, slots=True)
class CrawlReport:
    exit_code: ExitCode
    stats: Stats
    unique_urls: int  # in-scope URLs found, including the start URL
    finished: float  # the clock's reading at the end
    rejected: Counter[str] = field(default_factory=Counter)  # URLs not crawled, by rule
    examples: dict[str, list[str]] = field(default_factory=dict)  # a few for each rule

    @property
    def summary(self) -> str:
        return self.stats.summary(self.unique_urls, self.finished, self.rejected, self.examples)


async def crawl(
    cfg: Config,
    sink: Sink,
    *,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
) -> CrawlReport:
    """Crawl the start URL's host, writing a record per page to sink. A transport passed
    in stays open; sink is always closed. rng draws the jitter of retries' backoff."""
    start_url, scheme_was_implicit = normalize_start(cfg.start_url)
    stats = Stats(started=clock())
    frontier = Frontier()
    admission = Admission(frontier, Scope.for_url(start_url))
    owned = transport is None
    if transport is None:
        transport = AiohttpTransport(cfg)
    robots = None if cfg.ignore_robots else RobotsFetcher(transport, rng=rng or random.Random())
    fetcher = Fetcher(transport, cfg.max_page_bytes, stats)
    product = product_token(cfg.user_agent)
    start = _Start(
        admission=admission,
        frontier=frontier,
        fetcher=fetcher,
        robots=robots,
        product=product,
        strict_host=cfg.strict_host,
        stats=stats,
    )
    dispatcher = _Dispatcher(
        fetcher=fetcher,
        hops=AdmissionHops(admission),
        frontier=frontier,
        admission=admission,
        sink=sink,
        stats=stats,
        all_urls=cfg.all_urls,
        concurrency=min(INITIAL_CONCURRENCY, cfg.max_concurrency),
        max_pages=cfg.max_pages,
        robots=robots,
        product=product,
        robots_refresh=cfg.robots_refresh,
    )
    interrupts = _Interrupts(dispatcher)
    with interrupts:  # until the sink is closed, so a second Ctrl-C can cut closing short
        try:
            sink.start(dispatcher.pump)
            work = asyncio.create_task(_run(start, dispatcher, start_url, scheme_was_implicit))
            exit_code = await interrupts.wait(work)
        finally:
            try:
                await dispatcher.stop()
                if owned:
                    await transport.close()
            finally:
                sink.close()
    return CrawlReport(
        exit_code, stats, len(admission.seen), clock(), admission.rejected, admission.examples
    )


async def _run(
    start: _Start, dispatcher: _Dispatcher, start_url: str, scheme_was_implicit: bool
) -> ExitCode:
    started = await start.run(start_url, scheme_was_implicit)
    return ExitCode.CANNOT_START if started is None else await dispatcher.run(*started)


@dataclass(slots=True)
class _Interrupts:
    """Stops the crawl cleanly on the first SIGINT or SIGTERM (DESIGN.md §5.9).

    Dispatching stops and the crawl's task is cancelled; crawl() then cancels the fetches
    in flight and closes the sink, so only whole records are printed. A second SIGINT exits
    at once, even while closing the sink blocks the event loop. A signal the process
    inherited as ignored stays ignored, as nohup relies on.
    """

    dispatcher: _Dispatcher
    exit_code: ExitCode | None = None  # the first signal's
    _work: asyncio.Task[ExitCode] = field(init=False)
    _previous: dict[signal.Signals, _SignalHandler] = field(default_factory=dict)

    def __enter__(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in _STOP_SIGNALS:
            previous = signal.getsignal(sig)
            if previous is signal.SIG_IGN or previous is None:
                continue
            self._previous[sig] = previous
            loop.add_signal_handler(sig, self._received, sig)

    def __exit__(self, *exc_info: object) -> None:
        loop = asyncio.get_running_loop()
        for sig, previous in self._previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, previous)

    async def wait(self, work: asyncio.Task[ExitCode]) -> ExitCode:
        """work's exit code, or the signal's if one stopped it."""
        self._work = work
        try:
            return await work
        except asyncio.CancelledError:
            if self.exit_code is None:  # cancelled by crawl()'s caller, not by a signal
                raise
            return self.exit_code

    def _received(self, sig: signal.Signals) -> None:
        if self.exit_code is not None:  # a second SIGTERM: closing is already under way
            return
        self.exit_code = _STOP_SIGNALS[sig]
        self.dispatcher.halt()
        self._work.cancel()
        if signal.SIGINT in self._previous:
            signal.signal(signal.SIGINT, _exit_now)


def _exit_now(signum: int, frame: FrameType | None) -> NoReturn:
    """A second Ctrl-C: exit without waiting for anything."""
    os._exit(ExitCode.INTERRUPTED)


@dataclass(frozen=True, slots=True)
class _CantStart:
    message: str  # why, after "can't start: "
    no_answer: str | None = None  # the transport error kind, if the start URL got no answer


@dataclass(slots=True, kw_only=True)
class _Start:
    """Settles the start URL before anything else is crawled (DESIGN.md §2.4, §6.1).

    It reads robots.txt for every origin in scope, then fetches the start URL. Its redirects
    can still change the scope: one that only adds or removes "www." moves the crawl to that
    host, unless --strict-host, after reading that host's robots.txt; one to any other host
    ends the crawl, saying what to run instead. A start URL given without a scheme that
    gets no answer over https:// is tried once over http://.

    It is the start URL's hop policy, which is why fetcher.HopPolicy is async.
    """

    admission: Admission
    frontier: Frontier
    fetcher: Fetcher
    robots: RobotsFetcher | None  # None under --ignore-robots
    product: str  # the User-Agent's product token, which robots.txt groups name
    strict_host: bool
    stats: Stats
    _files: dict[str, RobotsTxt] = field(default_factory=dict, init=False)  # by origin
    _warned: set[str] = field(default_factory=set, init=False)  # unreachable, logged
    _problem: _CantStart | None = field(default=None, init=False)  # why a hop can't be followed

    async def run(self, start_url: str, scheme_was_implicit: bool) -> tuple[CrawlItem, Page] | None:
        """The start item and its page; or None, once it has logged why there isn't one."""
        started = await self._attempt(start_url)
        if (
            scheme_was_implicit
            and isinstance(started, _CantStart)
            and started.no_answer in _HTTPS_FAILURES
        ):
            plain = "http://" + start_url.removeprefix("https://")
            log.warning(
                "%s got no answer (%s); trying %s, since no scheme was given",
                start_url,
                started.no_answer,
                plain,
            )
            self.admission.seen.clear()  # forget the https:// attempt
            self.admission.scope = Scope.for_url(plain)
            started = await self._attempt(plain)
        if isinstance(started, _CantStart):
            log.error("can't start: %s", started.message)
            return None
        return started

    async def _attempt(self, start_url: str) -> tuple[CrawlItem, Page] | _CantStart:
        if (problem := await self._read_robots(self.admission.scope, start_url)) is not None:
            return problem
        self.admission.admit([(dedupe_key(start_url), start_url)], depth=0)
        item = self.frontier.pop()
        assert item is not None
        # The start URL is fetched on its own: if it isn't a page, there's no crawl.
        outcome = await self.fetcher.fetch(item, self)
        self.frontier.finish()
        # Nothing but the start URL's own chain has claimed anything yet.
        assert not isinstance(outcome, Claimed)
        if isinstance(outcome, Page):
            return item, outcome
        _count(self.stats, outcome)
        if self._problem is not None:
            return self._problem
        if isinstance(outcome, NotPage):
            content_type = outcome.content_type or "none"
            return _CantStart(f"{outcome.url} isn't an HTML page (Content-Type: {content_type})")
        unanswered = outcome.status is None and len(outcome.chain) == 1
        return _CantStart(self._explain(outcome), outcome.reason if unanswered else None)

    async def admit_hop(self, chain: Collection[str], url: str, key: str) -> Hop:
        admission = self.admission
        if not self.strict_host and (scope := admission.scope.www_switch(url)) is not None:
            log.warning(
                "the start URL redirects to %s: crawling %s instead of %s",
                url,
                scope.host,
                admission.scope.host,
            )
            admission.scope = scope
            if (problem := await self._read_robots(scope, url)) is not None:
                self._problem = problem
                return Hop.DISALLOWED
        return admission.admit_hop(chain, url, key)

    async def _read_robots(self, scope: Scope, url: str) -> _CantStart | None:
        """Read robots.txt for those of scope's origins not read yet, in parallel and before
        any page (DESIGN.md §5.7), and make it admission's policy. Returns why url, the start
        URL or where it redirects, can't be crawled, if it can't."""
        if self.robots is None:
            return None
        missing = [origin for origin in scope.origins if origin not in self._files]
        self._files.update(zip(missing, await self.robots.fetch_all(missing), strict=True))
        files = {origin: self._files[origin] for origin in scope.origins}
        policy = self.admission.robots = RobotsPolicy(self.product, files)
        file = policy.file_for(url)
        assert file is not None
        if file.no_answer is not None:  # the host itself is unreachable
            return _CantStart(f"{file.url} got no answer ({file.detail})", file.no_answer)
        if file.availability is Availability.UNREACHABLE:
            return _CantStart(
                f"{file.url} is unreachable ({file.detail}), so nothing may be crawled; "
                + _ROBOTS_HINT
            )
        if not file.allowed(url, self.product):
            crawler = self.product or "any crawler"
            return _CantStart(f"{file.url} disallows {url} for {crawler}; {_ROBOTS_HINT}")
        for origin, other in files.items():
            if other.availability is Availability.UNREACHABLE and origin not in self._warned:
                self._warned.add(origin)
                log.warning(
                    "%s is unreachable (%s): its URLs aren't crawled", other.url, other.detail
                )
        return None

    def _explain(self, final: Final) -> str:
        """Why a start URL that ended without a page can't start, and what to do instead."""
        if final.reason == "redirect out of scope" and final.location is not None:
            target = final.location
            scope = self.admission.scope
            if self.strict_host and scope.www_switch(target) is not None:
                return (
                    f"{final.url} redirects to {target}, and --strict-host keeps the crawl to "
                    f"{scope.host}; to crawl that host instead, run: crawl {target}"
                )
            return (
                f"{final.url} redirects to {target}, outside the crawl's scope; to crawl "
                f"there instead, run: crawl {target}"
            )
        return f"{final.url}: {final.detail}"


def _count(stats: Stats, outcome: NotPage | Claimed | Final) -> None:
    """Count an item that ended without a page."""
    if isinstance(outcome, NotPage):
        stats.not_html += 1
    elif isinstance(outcome, Claimed):
        # The page it leads to is printed once, by the item that claimed it.
        stats.failures["redirect to a URL already claimed"] += 1
    else:
        stats.failures[outcome.reason] += 1


@dataclass(slots=True, kw_only=True)
class _Dispatcher:
    """Starts a fetch whenever one finishes, until nothing is outstanding or, under
    --max-pages, until that many pages are printed."""

    fetcher: Fetcher
    hops: HopPolicy
    frontier: Frontier
    admission: Admission
    sink: Sink
    stats: Stats
    all_urls: bool
    concurrency: int
    max_pages: int | None  # stop once this many pages are printed
    robots: RobotsFetcher | None  # None under --ignore-robots
    product: str  # the User-Agent's product token, which robots.txt groups name
    robots_refresh: float  # seconds between fetches of robots.txt
    _ctx: WorkerContext = field(init=False)
    _in_flight: int = field(default=0, init=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _stopping: bool = field(default=False, init=False)
    _done: asyncio.Future[None] = field(init=False)
    _refresh: asyncio.TimerHandle | None = field(default=None, init=False)
    _refreshing: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._done = asyncio.get_running_loop().create_future()

    async def run(self, start: CrawlItem, page: Page) -> ExitCode:
        """Crawl from the start page until nothing is outstanding."""
        # The scope is fixed now, so the parsers' context can be too.
        self._ctx = WorkerContext(self.admission.scope.prefixes, self.all_urls)
        if self.robots is not None:
            self._refresh = asyncio.get_running_loop().call_later(
                self.robots_refresh, self._start_refresh
            )
        self._page(start, page)
        self.pump()
        await self._done
        return ExitCode.INTERNAL_ERROR if self.stats.internal_errors else ExitCode.OK

    def pump(self) -> None:
        """Start fetches while there's room. It runs after every completion, and when the
        sink's backlog clears."""
        if self._stopping:
            return
        while self._in_flight < self.concurrency and not self.sink.backlogged:
            # Under --max-pages, no page is fetched only to be thrown away (DESIGN.md §5.1).
            if self.max_pages is not None and self.stats.pages + self._in_flight >= self.max_pages:
                break
            item = self.frontier.pop()
            if item is None:
                break
            self._in_flight += 1
            task = asyncio.create_task(self._process(item))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        complete = self.frontier.outstanding == 0 or self.stats.pages == self.max_pages
        if complete and not self._done.done():
            self._done.set_result(None)

    def halt(self) -> None:
        """Start no more fetches."""
        self._stopping = True

    async def stop(self) -> None:
        """Cancel the fetches still running, and wait for them to end."""
        self.halt()
        if self._refresh is not None:
            self._refresh.cancel()
        tasks = list(self._tasks)
        if self._refreshing is not None:
            tasks.append(self._refreshing)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start_refresh(self) -> None:
        self._refresh = None
        self._refreshing = asyncio.create_task(self._refresh_robots())

    async def _refresh_robots(self) -> None:
        """Fetch every origin's robots.txt again, as RFC 9309 asks of a crawl that runs
        longer than a day. A file that's unreachable now keeps its old rules."""
        assert self.robots is not None
        try:
            current = self.admission.robots.files
            files = dict(current)
            fetched = await self.robots.fetch_all(list(current))
            for origin, file in zip(current, fetched, strict=True):
                if file.availability is Availability.UNREACHABLE:
                    log.warning("%s is unreachable (%s): keeping its rules", file.url, file.detail)
                else:
                    files[origin] = file
            self.admission.robots = RobotsPolicy(self.product, files)
        except Exception:
            self.stats.internal_errors += 1
            log.exception("internal error while refetching robots.txt")
        self._refresh = asyncio.get_running_loop().call_later(
            self.robots_refresh, self._start_refresh
        )

    async def _process(self, item: CrawlItem) -> None:
        try:
            outcome = await self.fetcher.fetch(item, self.hops)
            if isinstance(outcome, Page):
                self._page(item, outcome)
            else:
                _count(self.stats, outcome)
        except Exception:
            # A bug, not a property of the page. Count it, so the exit code says so, and
            # carry on with the rest of the crawl.
            self.stats.internal_errors += 1
            log.exception("internal error while crawling %s", item.url)
        finally:
            self._in_flight -= 1
            self.frontier.finish()
            self.pump()

    def _page(self, item: CrawlItem, page: Page) -> None:
        result = parse_page(page.job, self._ctx)
        self.sink.write(result.record)
        # The links are admitted before the page finishes, so that outstanding reaches 0
        # only when the crawl is complete (DESIGN.md §4.2).
        self.admission.admit(result.links, item.depth + 1)
        self.stats.pages += 1
        self.stats.links_found += result.links_found
