import asyncio
import itertools
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest

from tests.support.fake_transport import Answer, FakeResponse, FakeTransport, Reply
from tests.support.jitter import Fixed, Lowest
from web_crawler import engine
from web_crawler.config import Config
from web_crawler.engine import CrawlReport
from web_crawler.limiter import START as INITIAL_LIMIT
from web_crawler.limiter import ConcurrencyLaw
from web_crawler.output import MemorySink, Sink
from web_crawler.pool import InlineParser
from web_crawler.types import ErrorKind, ExitCode, ParseJob, ParseResult, WorkerContext

START = "https://example.com/"


async def crawl(cfg: Config, sink: Sink, **collaborators: Any) -> CrawlReport:
    """engine.crawl, parsing inline: these tests are about dispatch, and the parse gateway's
    own are in tests/integration/test_pool.py."""
    return await engine.crawl(cfg, sink, parser=InlineParser, **collaborators)


HTML = {"Content-Type": "text/html"}
TEXT = {"Content-Type": "text/plain"}
ROBOTS = "https://example.com/robots.txt"
PLAIN_ROBOTS = "http://example.com/robots.txt"


def pages(transport: FakeTransport) -> list[str]:
    """What the crawl got responses for, robots.txt aside."""
    return [url for url in transport.requested if not url.endswith("/robots.txt")]


def tries(transport: FakeTransport) -> list[str]:
    """What the crawl requested, answered or not, robots.txt aside."""
    return [url for url in transport.opened if not url.endswith("/robots.txt")]


def robots(rules: str) -> Reply:
    return Reply(headers=TEXT, body=rules.encode())


def html(*hrefs: str, delay: float = 0.0) -> Reply:
    return Reply(
        headers=HTML, body="".join(f'<a href="{h}">x</a>' for h in hrefs).encode(), delay=delay
    )


def fan_out(pages: int, delay: float) -> dict[str, Reply | ErrorKind]:
    """A start page linking to pages that each take delay seconds to answer."""
    replies: dict[str, Reply | ErrorKind] = {START: html(*(f"/{n}" for n in range(pages)))}
    replies |= {f"https://example.com/{n}": html(delay=delay) for n in range(pages)}
    return replies


async def test_prints_each_reachable_page_once() -> None:
    replies: dict[str, Reply | ErrorKind] = {
        START: html("/a", "/b", "https://elsewhere.org/"),
        "https://example.com/a": html("/", "/b", "/c"),
        "https://example.com/b": html("/a"),
        "https://example.com/c": html("/"),
    }
    transport, sink = FakeTransport(replies), MemorySink()
    report = await crawl(Config(start_url=START), sink, transport=transport)
    assert report.exit_code is ExitCode.OK
    assert sorted(sink.records) == sorted(
        [
            b"https://example.com/\n  https://example.com/a\n  https://example.com/b\n"
            b"  https://elsewhere.org/\n\n",
            b"https://example.com/a\n  https://example.com/\n  https://example.com/b\n"
            b"  https://example.com/c\n\n",
            b"https://example.com/b\n  https://example.com/a\n\n",
            b"https://example.com/c\n  https://example.com/\n\n",
        ]
    )
    assert sorted(pages(transport)) == sorted(replies)
    assert sink.closed
    assert not transport.closed  # a transport passed in belongs to the caller
    assert (report.stats.pages, report.stats.links_found, report.unique_urls) == (4, 8, 4)


@pytest.mark.parametrize(("max_concurrency", "expected"), [(64, 64), (20, 20), (3, 3)])
async def test_the_limit_doubles_up_to_the_ceiling_while_responses_stay_as_fast(
    max_concurrency: int, expected: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, "web_crawler.engine")
    transport = FakeTransport(fan_out(pages=300, delay=0.01))
    cfg = Config(start_url=START, max_concurrency=max_concurrency)
    report = await crawl(cfg, MemorySink(), transport=transport)
    assert report.stats.pages == 301
    assert transport.max_in_flight == expected
    if max_concurrency == 64:  # slow start: 8, 16, 32, then the ceiling
        grown = [record.getMessage() for record in caplog.records if "grow" in record.message]
        assert [line.split(", ")[0] for line in grown[:3]] == [
            "concurrency limit: grow: 8 -> 16",
            "concurrency limit: grow: 16 -> 32",
            "concurrency limit: grow: 32 -> 64",
        ]


def cuts(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.message for record in caplog.records if "concurrency limit: cut" in record.message
    ]


@pytest.mark.parametrize(
    ("answer", "cut", "retried"),
    [
        (Reply(429, HTML), True, True),
        (Reply(503, HTML), True, True),
        (ErrorKind.TIMEOUT, True, True),
        (ErrorKind.CONNECT_TIMEOUT, True, True),
        (ErrorKind.RESET, True, True),
        (ErrorKind.CONNECT, True, True),  # refused
        (Reply(headers=HTML, read_error=ErrorKind.PAYLOAD), True, True),  # cut off mid-body
        (Reply(500, HTML), False, True),
        (Reply(404, HTML), False, False),
        (ErrorKind.DNS, False, True),
        (ErrorKind.TLS, False, True),
    ],
)
async def test_distress_cuts_the_limit(
    answer: Answer, cut: bool, retried: bool, caplog: pytest.LogCaptureFixture
) -> None:
    # The page fails once; a page that's worth another try is printed the second time.
    caplog.set_level(logging.INFO, "web_crawler.engine")
    replies: dict[str, Answer | list[Answer]] = {**fan_out(pages=40, delay=0.01)}
    replies["https://example.com/7"] = [answer, html()]
    transport = FakeTransport(replies)
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport, rng=Lowest())
    assert report.stats.pages == 40 + retried
    assert cuts(caplog) == (["concurrency limit: cut: 8 -> 4"] if cut else [])


async def test_one_congestion_event_cuts_the_limit_once(caplog: pytest.LogCaptureFixture) -> None:
    # Eight requests in flight all get a 429: one cut answers them all. Tried again, after
    # the cut, they get their pages.
    caplog.set_level(logging.INFO, "web_crawler.engine")
    replies: dict[str, Answer | list[Answer]] = {**fan_out(pages=8, delay=0.01)}
    replies |= {
        f"https://example.com/{n}": [Reply(429, HTML, delay=0.01), html()] for n in range(8)
    }
    transport = FakeTransport(replies)
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport, rng=Lowest())
    assert cuts(caplog) == ["concurrency limit: cut: 8 -> 4"]
    assert report.stats.pages == 9


def test_samples_are_responses_below_500_other_than_429(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, "web_crawler.engine")
    limiter = engine._Limiter(ConcurrencyLaw(64), clock=lambda: 1.0)
    for status in (500, 502, 504):  # not samples, slow as they are
        limiter.answered(status, 9.0, issued_at=0.0)
    for status in (200, 301, 404, 410, 499, 200, 200, 200):
        limiter.answered(status, 0.1, issued_at=0.0)
    # The eight samples made the first round, which measured the server at low load.
    assert caplog.messages == ["concurrency limit: base: 8 -> 8, median 100.0 ms, base 100.0 ms"]


class Times(FakeTransport):
    """Records when each request started, by the real clock."""

    def __init__(self, replies: Mapping[str, Answer | list[Answer]]) -> None:
        super().__init__(replies)
        self.started: list[tuple[str, float]] = []

    @asynccontextmanager
    async def open(self, url: str) -> AsyncIterator[FakeResponse]:
        self.started.append((url, time.monotonic()))
        async with super().open(url) as response:
            yield response

    def gaps(self) -> list[float]:
        """Seconds between the starts of successive page requests after the start page."""
        times = [at for url, at in self.started if not url.endswith("/robots.txt")][1:]
        return [later - earlier for earlier, later in itertools.pairwise(times)]


async def test_max_rps_spaces_requests_evenly() -> None:
    transport = Times(fan_out(pages=6, delay=0.0))
    report = await crawl(Config(start_url=START, max_rps=50), MemorySink(), transport=transport)
    assert report.stats.pages == 7
    gaps = transport.gaps()
    assert len(gaps) == 5
    assert min(gaps) >= 0.02 - 0.002  # 1 / 50 s, give or take the event loop's clock


@pytest.mark.parametrize("ignore_robots", [False, True])
async def test_robots_txt_crawl_delay_spaces_requests(
    ignore_robots: bool, caplog: pytest.LogCaptureFixture
) -> None:
    replies = fan_out(pages=4, delay=0.0)
    replies[ROBOTS] = robots("User-agent: *\nCrawl-delay: 0.05\n")
    transport = Times(replies)
    cfg = Config(start_url=START, max_rps=1000, ignore_robots=ignore_robots)
    await crawl(cfg, MemorySink(), transport=transport)
    if ignore_robots:
        assert max(transport.gaps()) < 0.05
        assert "Crawl-delay" not in caplog.text
    else:
        assert min(transport.gaps()) >= 0.05 - 0.002
        assert (
            "robots.txt asks for 0.05 s between requests (Crawl-delay), so a request starts "
            "every 0.05 s at most; --ignore-robots ignores it" in caplog.text
        )


async def test_max_rps_wins_over_a_longer_crawl_delay_only_when_lower(
    caplog: pytest.LogCaptureFixture,
) -> None:
    replies = fan_out(pages=3, delay=0.0)
    replies[ROBOTS] = robots("User-agent: *\nCrawl-delay: 0.01\n")
    transport = Times(replies)
    await crawl(Config(start_url=START, max_rps=20), MemorySink(), transport=transport)
    assert min(transport.gaps()) >= 0.05 - 0.002  # --max-rps 20 is the lower rate
    assert "Crawl-delay" not in caplog.text


class Stalled:
    """A parse gateway of two workers that holds every parse but the start page's until
    told to go."""

    workers = 2

    def __init__(self, ctx: WorkerContext) -> None:
        self._inline = InlineParser(ctx)
        self.go = asyncio.Event()
        self.waiting = 0

    async def parse(self, job: ParseJob) -> ParseResult:
        if job.url != START:
            self.waiting += 1
            await self.go.wait()
        return await self._inline.parse(job)

    async def close(self) -> None:
        pass

    def kill(self) -> None:
        pass


async def test_pipeline_slots_bound_the_pages_held_while_parsing_is_slow() -> None:
    # A fast server and slow parsing would otherwise fetch pages without bound.
    stalled: list[Stalled] = []

    def parser(ctx: WorkerContext) -> Stalled:
        stalled.append(Stalled(ctx))
        return stalled[0]

    transport = FakeTransport(fan_out(pages=40, delay=0.0))
    cfg = Config(start_url=START, max_concurrency=4)
    task = asyncio.create_task(engine.crawl(cfg, MemorySink(), transport=transport, parser=parser))
    await settle()
    # 4 requests in flight at most, and 4 + 4 * 2 workers items held, fetched or not.
    assert len(pages(transport)) == 1 + 12
    assert stalled[0].waiting == 12
    stalled[0].go.set()
    report = await task
    assert report.stats.pages == 41


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (ErrorKind.CONNECT, "can't start: https://example.com/: connect: scripted"),
        (Reply(404, HTML), "can't start: https://example.com/: HTTP 404"),
        (
            Reply(headers={"Content-Type": "image/png"}),
            "can't start: https://example.com/ isn't an HTML page (Content-Type: image/png)",
        ),
        (Reply(headers={}), "isn't an HTML page (Content-Type: none)"),
        (
            Reply(301, {"Location": "https://blog.example.com/"}),
            "can't start: https://example.com/ redirects to https://blog.example.com/, outside "
            "the crawl's scope; to crawl there instead, run: crawl https://blog.example.com/",
        ),
    ],
)
async def test_a_start_url_that_isnt_a_page_cant_start(
    reply: Reply | ErrorKind, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    sink = MemorySink()
    transport = FakeTransport({START: reply})
    report = await crawl(Config(start_url=START), sink, transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.CANNOT_START
    assert message in caplog.text
    assert sink.records == []
    assert sink.closed


async def test_an_internal_error_is_counted_and_the_crawl_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Flaky(MemorySink):
        def write(self, record: bytes) -> None:
            if record.startswith(b"https://example.com/bad\n"):
                raise RuntimeError("a bug")
            super().write(record)

    replies: dict[str, Reply | ErrorKind] = {
        START: html("/bad", "/good"),
        "https://example.com/bad": html("/after-bad"),
        "https://example.com/good": html(),
    }
    sink = Flaky()
    report = await crawl(Config(start_url=START), sink, transport=FakeTransport(replies))
    assert report.exit_code is ExitCode.INTERNAL_ERROR
    assert report.stats.internal_errors == 1
    assert len(sink.records) == 2  # the start page and /good
    assert "internal error while crawling https://example.com/bad" in caplog.text
    assert "RuntimeError: a bug" in caplog.text
    assert caplog.records[0].levelno == logging.ERROR


async def test_all_urls_prints_every_url_but_crawls_only_links() -> None:
    replies: dict[str, Reply | ErrorKind] = {
        START: Reply(
            headers=HTML,
            body=b'<img src="/photo.jpg"><a href="/page">p</a><form action="/search"></form>',
        ),
        "https://example.com/page": html(),
    }
    transport, sink = FakeTransport(replies), MemorySink()
    report = await crawl(Config(start_url=START, all_urls=True), sink, transport=transport)
    assert sorted(sink.records) == [
        b"https://example.com/\n  https://example.com/photo.jpg\n  https://example.com/page\n"
        b"  https://example.com/search\n\n",
        b"https://example.com/page\n\n",
    ]
    assert pages(transport) == [START, "https://example.com/page"]
    assert report.stats.links_found == 3


async def test_a_page_reached_by_redirect_is_fetched_and_printed_once() -> None:
    # /a and /b both redirect to /page, which /c links to as well, later.
    replies: dict[str, Reply | ErrorKind] = {
        START: html("/a", "/b", "/c"),
        "https://example.com/a": Reply(301, {"Location": "/page"}),
        "https://example.com/b": Reply(302, {"Location": "/page"}),
        "https://example.com/c": html("/page", delay=0.01),
        "https://example.com/page": html(),
    }
    transport, sink = FakeTransport(replies), MemorySink()
    report = await crawl(Config(start_url=START), sink, transport=transport)
    assert sorted(pages(transport)) == sorted(replies)  # each once
    assert sorted(sink.records) == sorted(
        [
            b"https://example.com/\n  https://example.com/a\n  https://example.com/b\n"
            b"  https://example.com/c\n\n",
            b"https://example.com/c\n  https://example.com/page\n\n",
            b"https://example.com/page\n\n",
        ]
    )
    assert report.stats.failures == {"redirect to a URL already claimed": 1}
    assert report.unique_urls == 5


async def test_failures_are_counted_not_printed() -> None:
    replies: dict[str, Reply | ErrorKind] = {
        START: html("/missing", "/image", "/down", "/off"),
        "https://example.com/image": Reply(headers={"Content-Type": "image/png"}),
        "https://example.com/down": ErrorKind.RESET,
        "https://example.com/off": Reply(302, {"Location": "https://elsewhere.org/"}),
    }
    sink = MemorySink()
    transport = FakeTransport(replies)
    report = await crawl(Config(start_url=START), sink, transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.OK
    assert len(sink.records) == 1
    assert report.stats.not_html == 1
    assert report.stats.failures == {"HTTP 404": 1, "reset": 1, "redirect out of scope": 1}
    assert report.stats.retries == {"reset": 3}  # its retries, before it failed


class Valve(MemorySink):
    """A sink that reports a backlog once it holds `limit` records, until opened."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.on_drained: Callable[[], object] = lambda: None

    def start(self, on_drained: Callable[[], object]) -> None:
        self.on_drained = on_drained

    @property
    def backlogged(self) -> bool:
        return len(self.records) >= self.limit

    def open(self) -> None:
        self.limit = 1 << 30
        asyncio.get_running_loop().call_soon(self.on_drained)


async def settle() -> None:
    """Run the event loop until the fake transport's work is done."""
    for _ in range(100):
        await asyncio.sleep(0)


async def test_a_backlogged_sink_pauses_dispatch_until_it_drains() -> None:
    transport = FakeTransport(fan_out(pages=20, delay=0.0))
    sink = Valve(limit=5)
    task = asyncio.create_task(crawl(Config(start_url=START), sink, transport=transport))
    await settle()
    # Fetches already in flight finish and print, but no new one starts.
    paused_at = len(pages(transport))
    assert len(sink.records) >= sink.limit
    assert paused_at < 21
    await settle()
    assert len(pages(transport)) == paused_at
    assert not task.done()
    sink.open()
    report = await task
    assert report.stats.pages == 21
    assert len(pages(transport)) == 21


async def test_cancelling_a_crawl_stops_its_fetches_and_closes_the_sink() -> None:
    transport = FakeTransport(fan_out(pages=20, delay=60))
    sink = MemorySink()
    task = asyncio.create_task(crawl(Config(start_url=START), sink, transport=transport))
    await settle()
    assert transport.in_flight == INITIAL_LIMIT
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.in_flight == 0
    assert sink.closed
    assert len(sink.records) == 1  # the start page, whole


async def test_reports_the_clock() -> None:
    times = iter([100.0])  # when the crawl starts; it's 102.5 from then on
    report = await crawl(
        Config(start_url=START),
        MemorySink(),
        transport=FakeTransport({START: html()}),
        clock=lambda: next(times, 102.5),
    )
    assert report.summary.startswith("1 page in 2.5 s (0.4 pages/s), 0 links found")


async def test_reads_robots_txt_for_both_origins_before_the_start_page() -> None:
    transport = FakeTransport({START: html()})
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport)
    assert report.exit_code is ExitCode.OK
    assert sorted(transport.requested[:2]) == [PLAIN_ROBOTS, ROBOTS]  # 404s: no rules
    assert transport.requested[2:] == [START]


async def test_does_not_crawl_what_robots_txt_disallows() -> None:
    replies: dict[str, Answer | list[Answer]] = {
        ROBOTS: robots("User-agent: *\nDisallow: /private\n"),
        START: html("/private/a", "/private/b", "/public", "/redirect"),
        "https://example.com/public": html(),
        "https://example.com/redirect": Reply(302, {"Location": "/private/c"}),
    }
    transport, sink = FakeTransport(replies), MemorySink()
    report = await crawl(Config(start_url=START), sink, transport=transport)
    assert report.exit_code is ExitCode.OK
    assert sorted(pages(transport)) == [
        START,
        "https://example.com/public",
        "https://example.com/redirect",
    ]
    # The links are still printed.
    assert sink.records[0].count(b"/private/") == 2
    assert report.rejected == {"robots.txt": 3}
    assert report.examples == {
        "robots.txt": [
            "https://example.com/private/a",
            "https://example.com/private/b",
            "https://example.com/private/c",
        ]
    }
    assert report.stats.failures == {"redirect disallowed": 1}
    assert "not crawled: robots.txt (3, such as https://example.com/private/a)" in report.summary


async def test_robots_txt_for_another_origin_applies_to_it_alone() -> None:
    replies: dict[str, Answer | list[Answer]] = {
        PLAIN_ROBOTS: robots("User-agent: *\nDisallow: /\n"),
        START: html("http://example.com/plain", "/secure"),
        "https://example.com/secure": html(),
    }
    transport = FakeTransport(replies)
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport)
    assert sorted(pages(transport)) == [START, "https://example.com/secure"]
    assert report.rejected == {"robots.txt": 1}


async def test_an_unreachable_robots_txt_on_the_other_origin_disallows_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    replies: dict[str, Answer | list[Answer]] = {
        PLAIN_ROBOTS: ErrorKind.CONNECT,
        START: html("http://example.com/plain", "/secure"),
        "https://example.com/secure": html(),
    }
    transport = FakeTransport(replies)
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.OK
    assert sorted(pages(transport)) == [START, "https://example.com/secure"]
    assert report.rejected == {"robots.txt": 1}
    assert (
        "http://example.com/robots.txt is unreachable (connect: scripted, 3 tries): "
        "its URLs aren't crawled" in caplog.text
    )


@pytest.mark.parametrize(
    ("robots_txt", "message"),
    [
        (
            robots("User-agent: *\nDisallow: /\n"),
            "can't start: https://example.com/robots.txt disallows https://example.com/ for "
            "web-crawler; if the site is yours, --ignore-robots skips robots.txt",
        ),
        (
            Reply(503, TEXT),
            "can't start: https://example.com/robots.txt is unreachable (HTTP 503, 3 tries), "
            "so nothing may be crawled; if the site is yours, --ignore-robots skips robots.txt",
        ),
        # No answer at all: the host is down, which --ignore-robots wouldn't help.
        (
            ErrorKind.CONNECT,
            "can't start: https://example.com/robots.txt got no answer "
            "(connect: scripted, 3 tries)",
        ),
    ],
)
async def test_a_start_url_robots_txt_keeps_from_crawling_cant_start(
    robots_txt: Answer, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    transport = FakeTransport({ROBOTS: robots_txt, START: html()})
    sink = MemorySink()
    report = await crawl(Config(start_url=START), sink, transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.CANNOT_START
    assert message in caplog.text
    assert pages(transport) == []
    assert sink.records == []


async def test_a_custom_user_agent_selects_its_own_group() -> None:
    replies: dict[str, Answer | list[Answer]] = {
        ROBOTS: robots("User-agent: MyBot\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n"),
        START: html("/a", "/b"),
    }
    transport = FakeTransport(replies)
    cfg = Config(start_url=START, user_agent="MyBot/2.0")
    await crawl(cfg, MemorySink(), transport=transport)
    assert pages(transport) == [START, "https://example.com/b"]


async def test_ignore_robots_fetches_no_robots_txt() -> None:
    replies: dict[str, Answer | list[Answer]] = {
        ROBOTS: robots("User-agent: *\nDisallow: /\n"),
        START: html("/page"),
        "https://example.com/page": html(),
    }
    transport = FakeTransport(replies)
    report = await crawl(
        Config(start_url=START, ignore_robots=True), MemorySink(), transport=transport
    )
    assert transport.requested == [START, "https://example.com/page"]
    assert report.stats.pages == 2


def slow_chain(length: int, delay: float) -> dict[str, Answer | list[Answer]]:
    """START, then pages /1 to /length, each linking to the next and slow to answer."""
    replies: dict[str, Answer | list[Answer]] = {START: html("/1")}
    for n in range(1, length + 1):
        replies[f"https://example.com/{n}"] = html(f"/{n + 1}", delay=delay)
    return replies


async def test_refetches_robots_txt_while_a_crawl_runs() -> None:
    # robots.txt is refetched after 0.05 s, and disallows /4 now; /4 is found after 0.18 s.
    replies = slow_chain(5, delay=0.06)
    replies[ROBOTS] = [robots(""), robots("User-agent: *\nDisallow: /4\n")]
    transport = FakeTransport(replies)
    cfg = Config(start_url=START, robots_refresh=0.05)
    report = await crawl(cfg, MemorySink(), transport=transport)
    assert transport.requested.count(ROBOTS) >= 2
    assert pages(transport) == [START, *(f"https://example.com/{n}" for n in (1, 2, 3))]
    assert report.rejected == {"robots.txt": 1}


async def test_a_refetch_that_fails_keeps_the_rules(caplog: pytest.LogCaptureFixture) -> None:
    replies = slow_chain(5, delay=0.06)
    replies[ROBOTS] = [robots("User-agent: *\nDisallow: /4\n"), Reply(503, TEXT)]
    transport = FakeTransport(replies)
    cfg = Config(start_url=START, robots_refresh=0.05)
    report = await crawl(cfg, MemorySink(), transport=transport, rng=Lowest())
    assert report.rejected == {"robots.txt": 1}
    assert (
        "https://example.com/robots.txt is unreachable (HTTP 503, 3 tries): keeping its rules"
        in caplog.text
    )


class Broken(FakeTransport):
    """Fails with a bug, rather than a transport error, from the second robots.txt round."""

    @asynccontextmanager
    async def open(self, url: str) -> AsyncIterator[FakeResponse]:
        if url.endswith("/robots.txt") and self.requested.count(url) >= 1:
            raise RuntimeError("a bug")
        async with super().open(url) as response:
            yield response


async def test_a_bug_in_a_refetch_is_counted_and_the_crawl_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = Broken(slow_chain(4, delay=0.06))
    cfg = Config(start_url=START, robots_refresh=0.05)
    report = await crawl(cfg, MemorySink(), transport=transport)
    assert report.exit_code is ExitCode.INTERNAL_ERROR
    assert report.stats.internal_errors >= 1
    assert report.stats.pages == 5
    assert "internal error while refetching robots.txt" in caplog.text


async def test_a_fetch_that_fails_is_tried_again(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, "web_crawler.engine")
    replies: dict[str, Answer | list[Answer]] = {
        START: html("/a"),
        "https://example.com/a": [ErrorKind.RESET, Reply(502, HTML), html()],
    }
    transport, sink = FakeTransport(replies), MemorySink()
    report = await crawl(Config(start_url=START), sink, transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.OK
    assert len(sink.records) == 2
    assert tries(transport) == [START] + ["https://example.com/a"] * 3
    assert report.stats.retries == {"reset": 1, "HTTP 502": 1}
    assert not report.stats.failures
    assert "retried: HTTP 502 (1), reset (1)" in report.summary
    assert "https://example.com/a: reset: scripted; trying again in 0.00 s" in caplog.messages


@pytest.mark.parametrize("retries", [0, 1, 3])
async def test_a_fetch_that_keeps_failing_ends_after_its_retries(retries: int) -> None:
    replies: dict[str, Answer | list[Answer]] = {
        START: html("/a"),
        "https://example.com/a": ErrorKind.CONNECT,
    }
    transport = FakeTransport(replies)
    cfg = Config(start_url=START, retries=retries)
    report = await crawl(cfg, MemorySink(), transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.OK
    assert tries(transport) == [START] + ["https://example.com/a"] * (1 + retries)
    assert report.stats.failures == {"connect": 1}
    assert report.stats.retries == ({"connect": retries} if retries else {})


async def test_throttling_is_tried_again_on_a_budget_of_its_own() -> None:
    # Five tries more whatever --retries says, and none of them use up its retries.
    replies: dict[str, Answer | list[Answer]] = {
        START: html("/busy", "/flaky"),
        "https://example.com/busy": Reply(503, HTML),
        "https://example.com/flaky": [Reply(429, HTML), ErrorKind.RESET, Reply(429, HTML), html()],
    }
    transport = FakeTransport(replies)
    cfg = Config(start_url=START, retries=1)
    report = await crawl(cfg, MemorySink(), transport=transport, rng=Lowest())
    assert tries(transport).count("https://example.com/busy") == 6
    assert tries(transport).count("https://example.com/flaky") == 4
    assert report.stats.pages == 2
    assert report.stats.failures == {"HTTP 503": 1}
    assert report.stats.retries == {"HTTP 503": 5, "HTTP 429": 2, "reset": 1}


async def test_retry_after_pauses_every_request(caplog: pytest.LogCaptureFixture) -> None:
    # The server asks for a second's pause. The requests already in flight finish, but no
    # other starts until the pause is over, and then the page that asked for it goes first.
    caplog.set_level(logging.INFO, "web_crawler.engine")
    replies: dict[str, Answer | list[Answer]] = {
        START: html("/busy", *(f"/{n}" for n in range(30))),
        "https://example.com/busy": [Reply(429, {"Retry-After": "1"}), html()],
    }
    replies |= {f"https://example.com/{n}": html(delay=0.01) for n in range(30)}
    transport = Times(replies)
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport, rng=Lowest())
    assert report.stats.pages == 32
    started = [(url, at) for url, at in transport.started if not url.endswith("/robots.txt")]
    first, after = started[1 : 1 + INITIAL_LIMIT], started[1 + INITIAL_LIMIT :]
    assert first[0][0] == "https://example.com/busy"
    assert after[0][0] == "https://example.com/busy"
    assert after[0][1] - first[0][1] >= 1.0 - 0.01
    assert "https://example.com/busy: HTTP 429, Retry-After: pausing for 1 s" in caplog.messages


async def test_the_start_url_waits_as_long_as_retry_after_says(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, "web_crawler.engine")
    transport = Times({START: [Reply(503, {"Retry-After": "1"}), html()]})
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.OK
    (_, first), (_, second) = [entry for entry in transport.started if entry[0] == START]
    assert second - first >= 1.0 - 0.01
    assert "https://example.com/: HTTP 503; trying again in 1.00 s" in caplog.messages


async def test_an_error_that_would_recur_is_not_tried_again() -> None:
    replies: dict[str, Answer | list[Answer]] = {
        START: html("/a", "/b"),
        "https://example.com/a": ErrorKind.PROTOCOL,
        "https://example.com/b": Reply(404, HTML),
    }
    transport = FakeTransport(replies)
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport)
    assert sorted(tries(transport)) == sorted(replies)
    assert report.stats.failures == {"protocol": 1, "HTTP 404": 1}
    assert not report.stats.retries


async def test_an_item_waits_out_its_backoff_holding_no_permit() -> None:
    # With one request in flight at most, /bad's backoff (a tenth of 0.5 s) leaves room
    # for the other pages, each 0.01 s, before /bad is tried again.
    replies: dict[str, Answer | list[Answer]] = {
        START: html("/bad", "/1", "/2", "/3"),
        "https://example.com/bad": [ErrorKind.RESET, html()],
    }
    replies |= {f"https://example.com/{n}": html(delay=0.01) for n in (1, 2, 3)}
    transport = FakeTransport(replies)
    cfg = Config(start_url=START, max_concurrency=1)
    report = await crawl(cfg, MemorySink(), transport=transport, rng=Fixed(0.1))
    assert report.stats.pages == 5
    assert tries(transport) == [
        START,
        "https://example.com/bad",
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/3",
        "https://example.com/bad",
    ]


async def test_a_retry_resumes_after_the_redirects_it_followed() -> None:
    # /old's redirect claimed /new for it. Starting again from /old would find /new claimed,
    # and nothing would print it.
    replies: dict[str, Answer | list[Answer]] = {
        START: html("/old"),
        "https://example.com/old": Reply(301, {"Location": "/new"}),
        "https://example.com/new": [ErrorKind.TIMEOUT, html()],
    }
    transport, sink = FakeTransport(replies), MemorySink()
    report = await crawl(Config(start_url=START), sink, transport=transport, rng=Lowest())
    assert tries(transport) == [
        START,
        "https://example.com/old",
        "https://example.com/new",
        "https://example.com/new",
    ]
    assert sorted(sink.records)[-1] == b"https://example.com/new\n\n"
    assert not report.stats.failures


async def test_the_start_url_is_tried_again_before_the_crawl_gives_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport({START: [ErrorKind.RESET, Reply(500, HTML), html()]})
    report = await crawl(Config(start_url=START), MemorySink(), transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.OK
    assert tries(transport) == [START] * 3
    assert report.stats.retries == {"reset": 1, "HTTP 500": 1}

    transport = FakeTransport({START: ErrorKind.RESET})
    cfg = Config(start_url=START, retries=2)
    report = await crawl(cfg, MemorySink(), transport=transport, rng=Lowest())
    assert report.exit_code is ExitCode.CANNOT_START
    assert tries(transport) == [START] * 3
    assert "can't start: https://example.com/: reset: scripted" in caplog.text


async def test_records_stream_in_the_order_pages_complete() -> None:
    # DESIGN.md §2.4: output isn't sorted. /slow is linked first, and printed last.
    replies: dict[str, Answer | list[Answer]] = {
        START: html("/slow", "/fast"),
        "https://example.com/slow": html(delay=0.05),
        "https://example.com/fast": html(),
    }
    sink = MemorySink()
    await crawl(Config(start_url=START), sink, transport=FakeTransport(replies))
    assert [record.split(b"\n")[0] for record in sink.records] == [
        b"https://example.com/",
        b"https://example.com/fast",
        b"https://example.com/slow",
    ]
