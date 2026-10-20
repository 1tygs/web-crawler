"""The crawl: start-up, dispatch and termination (DESIGN.md §4.2, §4.3).

crawl() is the one place that builds concrete objects; everything else receives its
collaborators, so tests can swap in fakes. Start-up settles the start URL, its scope and
robots.txt before anything else is fetched. Then the dispatcher keeps as many fetches in
flight as the adaptive limit allows, and hands each page to the parse gateway: a pool of
processes, or the event loop itself. The first SIGINT or SIGTERM stops it cleanly.
"""

import asyncio
import functools
import logging
import os
import random
import signal
import time
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from http import HTTPStatus
from types import FrameType
from typing import Any, NoReturn

from web_crawler import retry
from web_crawler.config import Config
from web_crawler.fetcher import (
    AdmissionHops,
    Claimed,
    Fetcher,
    FetchOutcome,
    Final,
    HopPolicy,
    NotPage,
    Page,
    Retry,
    RobotsFetcher,
    resume,
)
from web_crawler.frontier import Admission, Frontier, Hop
from web_crawler.limiter import (
    BLOCKED_AFTER,
    OPENS_AFTER,
    BlockGuard,
    CircuitBreaker,
    ConcurrencyLaw,
    Decision,
    PauseGate,
    Probes,
    Verdict,
    rate_ceiling,
)
from web_crawler.output import Sink
from web_crawler.pool import ParseCrashed, Parser, worker_count
from web_crawler.pool import start as start_parser
from web_crawler.retry import Class
from web_crawler.robots import Availability, RobotsPolicy, RobotsTxt, product_token
from web_crawler.stats import Stats
from web_crawler.transport import AiohttpTransport, Transport
from web_crawler.types import CrawlItem, ErrorKind, ExitCode, WorkerContext
from web_crawler.urls import Scope, dedupe_key, normalize_start

log = logging.getLogger(__name__)

# Why a page whose parse crashed its worker, even alone, isn't printed.
PARSE_CRASHED = "parse crashed"

# What says a server is in distress, or the way to it congested, and cuts the concurrency
# limit (DESIGN.md §5.3): a refused connection, a timeout, a reset or a body cut off. DNS
# and TLS failures say nothing about load.
_THROTTLED = frozenset({HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.SERVICE_UNAVAILABLE})
_CONGESTED = frozenset(
    {
        ErrorKind.CONNECT,
        ErrorKind.CONNECT_TIMEOUT,
        ErrorKind.TIMEOUT,
        ErrorKind.RESET,
        ErrorKind.PAYLOAD,
    }
)

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


async def crawl(  # noqa: PLR0913  (the collaborators that tests replace)
    cfg: Config,
    sink: Sink,
    *,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
    parser: Callable[[WorkerContext], Parser] | None = None,
) -> CrawlReport:
    """Crawl the start URL's host, writing a record per page to sink. A transport passed
    in stays open; sink is always closed. rng draws the jitter of retries' backoff. parser
    makes the parse gateway, once the scope is known; by default it's a pool of
    cfg.parse_workers processes."""
    start_url, scheme_was_implicit = normalize_start(cfg.start_url)
    stats = Stats(started=clock())
    frontier = Frontier()
    admission = Admission(frontier, Scope.for_url(start_url))
    owned = transport is None
    if transport is None:
        transport = AiohttpTransport(cfg)
    rng = rng or random.Random()
    robots = None if cfg.ignore_robots else RobotsFetcher(transport, rng=rng)
    law = ConcurrencyLaw(cfg.max_concurrency)
    breaker = CircuitBreaker(first_probe=cfg.first_probe, give_up_after=cfg.give_up_after)
    guard = BlockGuard(first_probe=cfg.first_probe, give_up_after=cfg.give_up_after)
    signals = _Limiter(law, breaker, clock)
    fetcher = Fetcher(transport, cfg.max_page_bytes, stats, signals=signals, clock=clock)
    product = product_token(cfg.user_agent)
    if parser is None:
        workers = worker_count(cfg.parse_workers)
        parser = functools.partial(start_parser, workers=workers)
    start = _Start(
        admission=admission,
        frontier=frontier,
        fetcher=fetcher,
        robots=robots,
        product=product,
        strict_host=cfg.strict_host,
        stats=stats,
        retries=cfg.retries,
        rng=rng,
        clock=clock,
    )
    dispatcher = _Dispatcher(
        fetcher=fetcher,
        hops=AdmissionHops(admission),
        frontier=frontier,
        admission=admission,
        sink=sink,
        stats=stats,
        all_urls=cfg.all_urls,
        max_bytes=cfg.max_page_bytes,
        make_parser=parser,
        law=law,
        breaker=breaker,
        guard=guard,
        max_concurrency=cfg.max_concurrency,
        max_rps=cfg.max_rps,
        clock=clock,
        max_pages=cfg.max_pages,
        robots=robots,
        product=product,
        robots_refresh=cfg.robots_refresh,
        retries=cfg.retries,
        rng=rng,
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


class _Limiter:
    """Feeds the concurrency law and the circuit breaker what the fetcher hears (DESIGN.md
    §5.3), and logs what the law decides. Responses below 500, but for 429, are samples of
    the server's latency."""

    def __init__(
        self, law: ConcurrencyLaw, breaker: CircuitBreaker, clock: Callable[[], float]
    ) -> None:
        self._law = law
        self._breaker = breaker
        self._clock = clock

    def answered(self, status: int, ttfb: float, issued_at: float) -> None:
        self._breaker.answered(issued_at)
        if status in _THROTTLED:
            self._decided(self._law.on_congestion(issued_at, self._clock()))
        elif status < HTTPStatus.INTERNAL_SERVER_ERROR:
            self._decided(self._law.on_sample(ttfb, issued_at, self._clock()))

    def failed(self, kind: ErrorKind, issued_at: float) -> None:
        self._breaker.failed(kind, issued_at, self._clock())
        if kind in _CONGESTED:
            self._decided(self._law.on_congestion(issued_at, self._clock()))

    def _decided(self, decision: Decision | None) -> None:
        if decision is not None:
            log.info("concurrency limit: %s", decision)


async def _run(
    start: _Start, dispatcher: _Dispatcher, start_url: str, scheme_was_implicit: bool
) -> ExitCode:
    started = await start.run(start_url, scheme_was_implicit)
    return ExitCode.CANNOT_START if started is None else await dispatcher.run(*started)


@dataclass(slots=True)
class _Interrupts:
    """Stops the crawl cleanly on the first SIGINT or SIGTERM (DESIGN.md §5.9).

    Dispatching stops and the crawl's task is cancelled; crawl() then cancels the fetches
    in flight, lets the parses already submitted finish and print, stops the parser
    processes and closes the sink, so only whole records are printed. A second SIGINT kills
    the parser processes and exits at once, even while closing the sink blocks the event
    loop. A signal the process inherited as ignored stays ignored, as nohup relies on.
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
            signal.signal(signal.SIGINT, self._exit_now)

    def _exit_now(self, signum: int, frame: FrameType | None) -> NoReturn:
        """A second Ctrl-C: exit without waiting for anything. The parser processes ignore
        Ctrl-C, so they're killed first."""
        self.dispatcher.kill_parsers()
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
    retries: int  # for errors, from --retries
    rng: random.Random  # for retries' backoff
    clock: Callable[[], float]
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
        item = self.frontier.pop(self.clock())
        assert item is not None
        # The start URL is fetched on its own: if it isn't a page, there's no crawl. Nothing
        # else is queued, so its retries wait here.
        outcome = await self.fetcher.fetch(item, self)
        while isinstance(outcome, Retry):
            if (again := _next_attempt(outcome, self.retries, self.rng, self.stats)) is None:
                outcome = outcome.final
                break
            item, delay = again
            await asyncio.sleep(delay)
            outcome = await self.fetcher.fetch(item, self)
        # Nothing but the start URL's own chain has claimed anything yet.
        assert not isinstance(outcome, Claimed)
        if isinstance(outcome, Page):
            return item, outcome  # it's outstanding until the dispatcher parses its page
        self.frontier.finish()
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


def _next_attempt(
    outcome: Retry, retries: int, rng: random.Random, stats: Stats
) -> tuple[CrawlItem, float] | None:
    """When to fetch outcome's item again, and as what; or None if it has no retries left."""
    again = retry.next_attempt(
        outcome.item, outcome.cls, retries=retries, rng=rng, retry_after=outcome.retry_after
    )
    if again is not None:
        final = outcome.final
        stats.retries[final.reason] += 1
        log.info("%s: %s; trying again in %.2f s", final.url, final.detail, again[1])
    return again


def _answered(outcome: FetchOutcome) -> bool:
    """Whether outcome ended with a response, rather than with no response at all."""
    if isinstance(outcome, Retry):
        outcome = outcome.final
    return not isinstance(outcome, Final) or outcome.status is not None


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
    """Starts fetches while there's room, until nothing is outstanding or, under
    --max-pages, until that many pages are printed (DESIGN.md §4.2, §4.3).

    Room is a pipeline slot, which bounds the items held in memory, fetching or parsing; a
    permit from the concurrency limit, which bounds the requests in flight and is held only
    while fetching, so parsing never throttles fetching; and the rate ceiling's turn. Each
    item is one task: it fetches, then hands the page to the parse gateway."""

    fetcher: Fetcher
    hops: HopPolicy
    frontier: Frontier
    admission: Admission
    sink: Sink
    stats: Stats
    all_urls: bool
    max_bytes: int  # the most a page's body may decompress to
    make_parser: Callable[[WorkerContext], Parser]
    law: ConcurrencyLaw
    breaker: CircuitBreaker
    guard: BlockGuard
    max_concurrency: int
    max_rps: float | None
    clock: Callable[[], float]
    max_pages: int | None  # stop once this many pages are printed
    robots: RobotsFetcher | None  # None under --ignore-robots
    product: str  # the User-Agent's product token, which robots.txt groups name
    robots_refresh: float  # seconds between fetches of robots.txt
    retries: int  # for errors, from --retries
    rng: random.Random  # for retries' backoff
    _parser: Parser | None = field(default=None, init=False)
    _gate: PauseGate = field(default_factory=PauseGate, init=False)
    _slots: int = field(default=0, init=False)
    _in_pipeline: int = field(default=0, init=False)  # items dispatched and not finished
    _requests: int = field(default=0, init=False)  # fetches in progress: permits held
    _fetching: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _parsing: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _stopping: bool = field(default=False, init=False)
    _done: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake: asyncio.TimerHandle | None = field(default=None, init=False)
    _refresh: asyncio.TimerHandle | None = field(default=None, init=False)
    _refreshing: asyncio.Task[None] | None = field(default=None, init=False)
    _home: str = field(default="", init=False)  # the start page's URL, which probes request
    _unreachable: bool = field(default=False, init=False)  # the breaker, at the last pump
    _breaker_probes: asyncio.Task[None] | None = field(default=None, init=False)
    _guard_probes: asyncio.Task[None] | None = field(default=None, init=False)
    _aborted: bool = field(default=False, init=False)

    async def run(self, start: CrawlItem, page: Page) -> ExitCode:
        """Crawl from the start page until nothing is outstanding, or until the crawl gives
        up on its host."""
        # The scope is fixed now, so the parsers' context can be, and the gateway can start:
        # each worker gets the context once, as it starts (DESIGN.md §5.5).
        ctx = WorkerContext(self.admission.scope.prefixes, self.max_bytes, self.all_urls)
        self._parser = self.make_parser(ctx)
        # Room for every request the limit may allow, and for each worker's parse in
        # progress and a few queued for it.
        self._slots = self.max_concurrency + 4 * self._parser.workers
        self._set_rate()
        if self.robots is not None:
            self._refresh = asyncio.get_running_loop().call_later(
                self.robots_refresh, self._start_refresh
            )
        self._home = page.job.url
        self._start(start, page)
        self.pump()
        await self._done.wait()
        if self._aborted:
            return ExitCode.ABORTED
        return ExitCode.INTERNAL_ERROR if self.stats.internal_errors else ExitCode.OK

    def pump(self) -> None:
        """Start fetches while there's room. It runs after every completion and every
        release of a permit, when the rate ceiling allows the next start, when the sink's
        backlog clears, and when a probe finds the host well again."""
        if self._stopping:
            return
        if self.breaker.open is not self._unreachable:
            self._breaker_moved()
        while (
            not (self.breaker.open or self.guard.tripped)
            and self._in_pipeline < self._slots
            and self._requests < self.law.limit
            and not self.sink.backlogged
        ):
            # Under --max-pages, no page is fetched only to be thrown away (DESIGN.md §5.1).
            if (
                self.max_pages is not None
                and self.stats.pages + self._in_pipeline >= self.max_pages
            ):
                break
            now = self.clock()
            if (wait := self._gate.delay(now)) > 0:
                self._wake_in(wait)
                break
            item = self.frontier.pop(now)
            if item is None:
                if (due := self.frontier.next_retry_at()) is not None:
                    self._wake_in(due - now)
                elif self._in_pipeline == 0:
                    # Nothing else can settle a run of 403s now: they're final (DESIGN.md
                    # §5.4).
                    self._refused(self.guard.settle())
                break
            self._gate.take(now)
            self._start(item)
        complete = self.frontier.outstanding == 0 or self.stats.pages == self.max_pages
        if complete:
            self._done.set()

    def _breaker_moved(self) -> None:
        """The circuit breaker opened or closed since the last pump (DESIGN.md §5.3)."""
        self._unreachable = self.breaker.open
        if self._unreachable:
            log.warning(
                "%d requests in a row got no answer (%s): pausing the crawl, and checking %s "
                "until it answers, for up to %g s",
                OPENS_AFTER,
                self.breaker.last,
                self._home,
                self.breaker.probes.give_up_after,
            )
            self._breaker_probes = asyncio.create_task(self._probe_until_reachable())
            return
        log.warning("%s answers again: resuming the crawl", self._home)
        if self._breaker_probes is not None:  # a response closed it before a probe did
            self._breaker_probes.cancel()
            self._breaker_probes = None

    async def _probe_until_reachable(self) -> None:
        """Request the start page now and then while the breaker is open, until a response
        closes it; abort the crawl if none does in time."""
        breaker = self.breaker
        while breaker.open:
            if not await self._probe_due(breaker.probes):
                give_up = breaker.probes.give_up_after
                self._abort(f"{self._home} has given no answer for {give_up:g} s ({breaker.last})")
                return
            issued = self.clock()
            if await self.fetcher.probe(self._home) is None:
                breaker.probes.failed(self.clock())
            else:
                breaker.answered(issued)
        self._breaker_probes = None
        self.pump()

    async def _probe_until_served(self) -> None:
        """Request the start page now and then while the block guard is tripped, until it's
        served rather than refused, then try the refused pages again; abort the crawl if it
        isn't in time (DESIGN.md §5.4)."""
        guard = self.guard
        while guard.tripped:
            if not await self._probe_due(guard.probes):
                give_up = guard.probes.give_up_after
                self._abort(
                    f"{self._home} still answers 403 Forbidden after {give_up:g} s, as a block "
                    "would"
                )
                return
            status = await self.fetcher.probe(self._home)
            if status is None or status == HTTPStatus.FORBIDDEN:
                guard.probes.failed(self.clock())
                continue
            held = guard.recovered()
            log.warning(
                "%s is served again (HTTP %d): resuming the crawl, and trying the %d refused "
                "pages again",
                self._home,
                status,
                len(held),
            )
            self.stats.retries["HTTP 403"] += len(held)
            for item in held:
                self.frontier.schedule_retry(item, self.clock())
        self._guard_probes = None
        self.pump()

    async def _probe_due(self, probes: Probes) -> bool:
        """Wait until the next probe is due. Returns False if the time to give up comes
        first."""
        while (now := self.clock()) < probes.next:
            if now >= probes.deadline:
                return False
            await asyncio.sleep(min(probes.next, probes.deadline) - now)
        return now < probes.deadline

    def _abort(self, why: str) -> None:
        """Give up on the crawl: it ends as a signal ends it, keeping what it printed, and
        exits 4 (DESIGN.md §5.3)."""
        log.error("aborting: %s", why)
        self._aborted = True
        self.halt()
        self._done.set()

    def _start(self, item: CrawlItem, page: Page | None = None) -> None:
        self._in_pipeline += 1
        if page is None:
            self._requests += 1  # the fetch's permit, taken now, as the task runs later
        task = asyncio.create_task(self._process(item, page))
        self._fetching.add(task)
        task.add_done_callback(self._fetching.discard)
        task.add_done_callback(self._parsing.discard)

    def _wake_in(self, delay: float) -> None:
        """Pump after delay seconds, unless it's already due to run sooner: when the rate
        ceiling allows the next start, or the next retry is due."""
        loop = asyncio.get_running_loop()
        when = loop.time() + delay
        if self._wake is not None:
            if self._wake.when() <= when:
                return
            self._wake.cancel()
        self._wake = loop.call_at(when, self._woken)

    def _woken(self) -> None:
        self._wake = None
        self.pump()

    def _set_rate(self) -> None:
        """The rate ceiling: --max-rps, or robots.txt's Crawl-delay if it allows fewer."""
        delay = self.admission.robots.crawl_delay
        rate = rate_ceiling(self.max_rps, delay)
        if rate != self._gate.rate and delay and rate == 1 / delay:
            log.warning(
                "robots.txt asks for %g s between requests (Crawl-delay), so a request starts "
                "every %g s at most; --ignore-robots ignores it",
                delay,
                delay,
            )
        self._gate.rate = rate

    def halt(self) -> None:
        """Start no more fetches."""
        self._stopping = True

    async def stop(self) -> None:
        """Cancel the fetches still running; let the parses already submitted finish, so
        that their records are printed; then stop the parse gateway (DESIGN.md §5.9)."""
        self.halt()
        for timer in (self._wake, self._refresh):
            if timer is not None:
                timer.cancel()
        others = (self._refreshing, self._breaker_probes, self._guard_probes)
        cancelled = [*self._fetching, *(task for task in others if task is not None)]
        for task in cancelled:
            task.cancel()
        await asyncio.gather(*cancelled, *self._parsing, return_exceptions=True)
        if self._parser is not None:
            await self._parser.close()

    def kill_parsers(self) -> None:
        """Stop the parse gateway at once, for an exit that can't wait."""
        if self._parser is not None:
            self._parser.kill()

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
            self._set_rate()  # its Crawl-delay may have changed
        except Exception:
            self.stats.internal_errors += 1
            log.exception("internal error while refetching robots.txt")
        self._refresh = asyncio.get_running_loop().call_later(
            self.robots_refresh, self._start_refresh
        )

    async def _process(self, item: CrawlItem, page: Page | None) -> None:
        """Fetch item, unless its page is already here, and parse the page it leads to."""
        finished = True
        try:
            issued = self.clock()
            outcome = page if page is not None else await self._fetch(item)
            if isinstance(outcome, Page):
                self._refused(self.guard.answered(issued))
                # From here the task is a parse, which a stop lets finish and print rather
                # than cancel: once submitted, it runs to its end in its worker anyway.
                task = asyncio.current_task()
                assert task is not None
                self._fetching.discard(task)
                self._parsing.add(task)
                await self._page(item, outcome)
            else:
                finished = self._no_page(item, outcome, issued)
        except Exception:
            # A bug, not a property of the page. Count it, so the exit code says so, and
            # carry on with the rest of the crawl.
            self.stats.internal_errors += 1
            log.exception("internal error while crawling %s", item.url)
        finally:
            self._in_pipeline -= 1
            if finished:
                self.frontier.finish()
            self.pump()

    def _no_page(
        self, item: CrawlItem, outcome: NotPage | Claimed | Retry | Final, issued: float
    ) -> bool:
        """Deal with a fetch of item, sent at issued, that led to no page. Returns whether
        the item is finished: it isn't if it's to be tried again, or the block guard holds
        it."""
        if isinstance(outcome, Final) and outcome.status == HTTPStatus.FORBIDDEN:
            return not self._forbidden(item, outcome, issued)
        if _answered(outcome):  # by a response, and not a 403
            self._refused(self.guard.answered(issued))
        if isinstance(outcome, Retry):
            return not self._retry(outcome)
        _count(self.stats, outcome)
        return True

    def _forbidden(self, item: CrawlItem, final: Final, issued: float) -> bool:
        """item got a 403 for a request sent at issued, which the block guard may hold
        until its run of 403s is settled (DESIGN.md §5.4). Returns whether it did: then the
        item stays outstanding."""
        verdict = self.guard.forbidden(resume(item, final.chain), issued, self.clock())
        if verdict is Verdict.FINAL:
            _count(self.stats, final)
            return False
        if verdict is Verdict.TRIPPED:
            log.warning(
                "%d requests in a row were refused with 403 Forbidden, as a block would refuse "
                "them: pausing the crawl, and checking %s until it's served again, for up to "
                "%g s",
                BLOCKED_AFTER,
                self._home,
                self.guard.probes.give_up_after,
            )
            self._guard_probes = asyncio.create_task(self._probe_until_served())
        return True

    def _refused(self, held: list[CrawlItem]) -> None:
        """The block guard settled a run of 403s: the pages it held are forbidden."""
        for _ in held:
            self.stats.failures["HTTP 403"] += 1
            self.frontier.finish()

    def _retry(self, outcome: Retry) -> bool:
        """Put outcome's item in the retry heap, if it has retries left, and say whether it
        did; otherwise the item's failure is final. Waiting there, the item holds no permit
        and no pipeline slot (DESIGN.md §5.4). A Retry-After holds every request back."""
        now = self.clock()
        if outcome.cls is Class.ERROR and self.breaker.open:
            # The host can't be reached, which says nothing about this page: trying again
            # once it can costs the page none of its retries (DESIGN.md §5.3).
            final = outcome.final
            self.stats.retries[final.reason] += 1
            log.info("%s: %s; trying again once the host answers", final.url, final.detail)
            self.frontier.schedule_retry(outcome.item, now)
            return True
        if (pause := outcome.retry_after) is not None:
            # The server asks for no requests for a while, not just this one.
            final = outcome.final
            log.info("%s: %s, Retry-After: pausing for %g s", final.url, final.detail, pause)
            self._gate.pause(now + pause)
        again = _next_attempt(outcome, self.retries, self.rng, self.stats)
        if again is None:
            _count(self.stats, outcome.final)
            return False
        item, delay = again
        self.frontier.schedule_retry(item, now + delay)
        return True

    async def _fetch(self, item: CrawlItem) -> FetchOutcome:
        """item's fetch, which holds a permit of the concurrency limit until it's done."""
        try:
            return await self.fetcher.fetch(item, self.hops)
        finally:
            self._requests -= 1
            self.pump()

    async def _page(self, item: CrawlItem, page: Page) -> None:
        assert self._parser is not None
        try:
            result = await self._parser.parse(page.job)
        except ParseCrashed:
            log.warning("%s isn't printed: parsing it crashed the parser process", item.url)
            self.stats.failures[PARSE_CRASHED] += 1
            return
        self.stats.body_bytes += len(page.job.body)
        if result.error is not None:
            self.stats.failures[result.error] += 1
            return
        self.sink.write(result.record)
        # The links are admitted before the page finishes, so that outstanding reaches 0
        # only when the crawl is complete (DESIGN.md §4.2).
        self.admission.admit(result.links, item.depth + 1)
        self.stats.pages += 1
        self.stats.links_found += result.links_found
        self.stats.decoded_bytes += result.decoded_bytes
