import asyncio
import itertools
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest

from tests.support.fake_transport import Answer, FakeResponse, FakeTransport, Reply
from tests.support.jitter import Lowest
from web_crawler.config import Config
from web_crawler.engine import INITIAL_CONCURRENCY, crawl
from web_crawler.output import MemorySink
from web_crawler.transport import ErrorKind
from web_crawler.types import ExitCode

START = "https://example.com/"
HTML = {"Content-Type": "text/html"}
TEXT = {"Content-Type": "text/plain"}
ROBOTS = "https://example.com/robots.txt"
PLAIN_ROBOTS = "http://example.com/robots.txt"


def pages(transport: FakeTransport) -> list[str]:
    """What the crawl requested, robots.txt aside."""
    return [url for url in transport.requested if not url.endswith("/robots.txt")]


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


@pytest.mark.parametrize(("max_concurrency", "expected"), [(64, INITIAL_CONCURRENCY), (3, 3)])
async def test_keeps_a_fixed_number_of_fetches_in_flight(
    max_concurrency: int, expected: int
) -> None:
    transport = FakeTransport(fan_out(pages=40, delay=0.01))
    cfg = Config(start_url=START, max_concurrency=max_concurrency)
    report = await crawl(cfg, MemorySink(), transport=transport)
    assert report.stats.pages == 41
    assert transport.max_in_flight == expected


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
    report = await crawl(Config(start_url=START), sink, transport=FakeTransport({START: reply}))
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
    report = await crawl(Config(start_url=START), sink, transport=FakeTransport(replies))
    assert report.exit_code is ExitCode.OK
    assert len(sink.records) == 1
    assert report.stats.not_html == 1
    assert report.stats.failures == {"HTTP 404": 1, "reset": 1, "redirect out of scope": 1}


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
    assert transport.in_flight == INITIAL_CONCURRENCY
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.in_flight == 0
    assert sink.closed
    assert len(sink.records) == 1  # the start page, whole


async def test_reports_the_clock() -> None:
    ticks = itertools.count(100.0, 2.5)
    report = await crawl(
        Config(start_url=START),
        MemorySink(),
        transport=FakeTransport({START: html()}),
        clock=lambda: next(ticks),
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
