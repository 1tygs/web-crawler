"""The crawl: start-up, dispatch and termination (DESIGN.md §4.2, §4.3).

crawl() is the one place that builds concrete objects; everything else receives its
collaborators, so tests can swap in fakes. For now the dispatcher keeps a fixed number of
fetches in flight and parses each page inline on the event loop.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from web_crawler.config import Config
from web_crawler.fetcher import Fetcher, Final, NotPage, Page
from web_crawler.frontier import Admission, Frontier
from web_crawler.output import Sink
from web_crawler.parse import parse_page
from web_crawler.stats import Stats
from web_crawler.transport import AiohttpTransport, Transport
from web_crawler.types import CrawlItem, ExitCode, WorkerContext
from web_crawler.urls import Scope, normalize_start

log = logging.getLogger(__name__)

# The adaptive limiter's starting limit (DESIGN.md §5.3), held fixed until the limiter
# replaces it.
INITIAL_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class CrawlReport:
    exit_code: ExitCode
    stats: Stats
    unique_urls: int  # in-scope URLs found, including the start URL
    finished: float  # the clock's reading at the end

    @property
    def summary(self) -> str:
        return self.stats.summary(self.unique_urls, self.finished)


async def crawl(
    cfg: Config,
    sink: Sink,
    *,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CrawlReport:
    """Crawl the start URL's host, writing a record per page to sink. A transport passed
    in stays open; sink is always closed."""
    start_url, _scheme_was_implicit = normalize_start(cfg.start_url)
    scope = Scope.for_url(start_url)
    stats = Stats(started=clock())
    frontier = Frontier()
    admission = Admission(frontier)
    owned = transport is None
    if transport is None:
        transport = AiohttpTransport(cfg)
    dispatcher = _Dispatcher(
        fetcher=Fetcher(transport, scope, cfg.max_page_bytes, stats),
        frontier=frontier,
        admission=admission,
        sink=sink,
        stats=stats,
        ctx=WorkerContext(scope_prefixes=scope.prefixes),
        concurrency=min(INITIAL_CONCURRENCY, cfg.max_concurrency),
    )
    try:
        sink.start(dispatcher.pump)
        exit_code = await dispatcher.run(start_url)
    finally:
        try:
            await dispatcher.stop()
            if owned:
                await transport.close()
        finally:
            sink.close()
    return CrawlReport(exit_code, stats, len(admission.seen), clock())


@dataclass(slots=True, kw_only=True)
class _Dispatcher:
    """Starts a fetch whenever one finishes, until nothing is outstanding."""

    fetcher: Fetcher
    frontier: Frontier
    admission: Admission
    sink: Sink
    stats: Stats
    ctx: WorkerContext
    concurrency: int
    _in_flight: int = field(default=0, init=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _stopping: bool = field(default=False, init=False)
    _done: asyncio.Future[None] = field(init=False)

    def __post_init__(self) -> None:
        self._done = asyncio.get_running_loop().create_future()

    async def run(self, start_url: str) -> ExitCode:
        """Crawl from start_url until nothing is outstanding."""
        self.admission.admit([(start_url, start_url)], depth=0)
        start = self.frontier.pop()
        assert start is not None
        # The start URL is fetched on its own: if it isn't a page, there's no crawl.
        outcome = await self.fetcher.fetch(start)
        self.frontier.finish()
        if not isinstance(outcome, Page):
            self._count(outcome)
            log.error("can't start: %s", _explain(outcome))
            return ExitCode.CANNOT_START
        self._page(start, outcome)
        self.pump()
        await self._done
        return ExitCode.INTERNAL_ERROR if self.stats.internal_errors else ExitCode.OK

    def pump(self) -> None:
        """Start fetches while there's room. It runs after every completion, and when the
        sink's backlog clears."""
        if self._stopping:
            return
        while self._in_flight < self.concurrency and not self.sink.backlogged:
            item = self.frontier.pop()
            if item is None:
                break
            self._in_flight += 1
            task = asyncio.create_task(self._process(item))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if self.frontier.outstanding == 0 and not self._done.done():
            self._done.set_result(None)

    async def stop(self) -> None:
        """Cancel the fetches still running, and wait for them to end."""
        self._stopping = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _process(self, item: CrawlItem) -> None:
        try:
            outcome = await self.fetcher.fetch(item)
            if isinstance(outcome, Page):
                self._page(item, outcome)
            else:
                self._count(outcome)
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
        result = parse_page(page.job, self.ctx)
        self.sink.write(result.record)
        # The links are admitted before the page finishes, so that outstanding reaches 0
        # only when the crawl is complete (DESIGN.md §4.2).
        self.admission.admit(result.links, item.depth + 1)
        self.stats.pages += 1
        self.stats.links_found += result.links_found

    def _count(self, outcome: NotPage | Final) -> None:
        if isinstance(outcome, NotPage):
            self.stats.not_html += 1
        else:
            self.stats.failures[outcome.reason] += 1


def _explain(outcome: NotPage | Final) -> str:
    if isinstance(outcome, NotPage):
        content_type = outcome.content_type or "none"
        return f"{outcome.url} isn't an HTML page (Content-Type: {content_type})"
    return f"{outcome.url}: {outcome.detail}"
