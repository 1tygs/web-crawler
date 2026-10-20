import asyncio
import itertools
import logging
from collections.abc import Callable

import pytest

from tests.support.fake_transport import FakeTransport, Reply
from web_crawler.config import Config
from web_crawler.engine import INITIAL_CONCURRENCY, crawl
from web_crawler.output import MemorySink
from web_crawler.transport import ErrorKind
from web_crawler.types import ExitCode

START = "https://example.com/"
HTML = {"Content-Type": "text/html"}


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
    assert sorted(transport.requested) == sorted(replies)
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
            Reply(301, {"Location": "https://www.example.com/"}),
            "HTTP 301 to https://www.example.com/, outside the crawl's scope",
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
    paused_at = len(transport.requested)
    assert len(sink.records) >= sink.limit
    assert paused_at < 21
    await settle()
    assert len(transport.requested) == paused_at
    assert not task.done()
    sink.open()
    report = await task
    assert report.stats.pages == 21
    assert len(transport.requested) == 21


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
