"""Fault injection (DESIGN.md §11): servers that misbehave over real sockets, and a crawl that
recovers without losing a page, or ends as DESIGN.md §5.3, §5.4 and §6.1 say it should.

Faults in the parser processes, a worker killed mid-parse and a page that crashes its
worker, and Ctrl-C, are in test_crawl.py.
"""

import asyncio
import gzip
import time
from collections import Counter
from collections.abc import Mapping

import pytest
from aiohttp import web

from tests.support.command import CRAWL, run
from tests.support.jitter import Lowest
from tests.support.site import Handler, Resource, Site, page
from tests.support.waiting import until
from web_crawler.config import MIB, Config
from web_crawler.engine import crawl
from web_crawler.output import MemorySink
from web_crawler.types import ExitCode


async def test_a_connection_dropped_mid_body_is_tried_again() -> None:
    # The first time /page is asked for, the server sends half of its gzipped body and drops
    # the connection: the body is cut off, and the page is fetched again.
    body = gzip.compress(b'<a href="/after">after</a>' + bytes(10_000))
    asked: Counter[str] = Counter()

    async def flaky(request: web.Request) -> web.StreamResponse:
        asked[request.path] += 1
        response = web.StreamResponse(
            headers={"Content-Type": "text/html", "Content-Encoding": "gzip"}
        )
        response.content_length = len(body)
        await response.prepare(request)
        if asked[request.path] == 1:
            await response.write(body[: len(body) // 2])
            assert request.transport is not None
            request.transport.abort()
            return response
        await response.write(body)
        return response

    resources: dict[str, Resource | Handler] = {
        "/": page("/page"),
        "/page": flaky,
        "/after": page(),
    }
    async with Site(resources) as site:
        sink = MemorySink()
        cfg = Config(start_url=site.url(), ignore_robots=True)
        report = await crawl(cfg, sink, rng=Lowest())
    assert report.exit_code is ExitCode.OK
    assert f"{site.url('/page')}\n  {site.url('/after')}\n\n".encode() in sink.records
    assert site.targets().count("/page") == 2
    assert report.stats.retries == {"payload": 1}
    assert not report.stats.failures


async def test_a_gzip_stream_that_ends_early_is_parsed_as_far_as_it_got() -> None:
    # The body arrives whole, as its Content-Length says, but the gzip stream in it stops
    # halfway: what it decodes to is printed, as a browser shows it, and it isn't retried.
    links = "".join(f'<a href="/{n}">{n}</a>' for n in range(200)).encode()
    whole = gzip.compress(links)
    cut = Resource(
        body=whole[: len(whole) // 2],
        headers=(("Content-Type", "text/html"), ("Content-Encoding", "gzip")),
    )
    async with Site({"/": page("/cut"), "/cut": cut}) as site:
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url(), ignore_robots=True), sink)
    assert report.exit_code is ExitCode.OK
    [record] = [record for record in sink.records if record.startswith(site.url("/cut").encode())]
    printed = record.count(b"\n  ")
    assert 0 < printed < 200
    assert report.stats.truncated == 1
    assert site.targets().count("/cut") == 1
    assert "1 cut short" in report.summary


async def test_429_and_503_hold_the_crawl_back_as_long_as_retry_after_says() -> None:
    # /busy answers 429 with Retry-After: 1, then its page; /down answers 503 twice, with
    # no Retry-After, then its page. No request starts during the pause, and every page is
    # printed.
    asked: Counter[str] = Counter()

    async def busy(request: web.Request) -> web.StreamResponse:
        asked["busy"] += 1
        if asked["busy"] == 1:
            return web.Response(status=429, headers={"Retry-After": "1"})
        return web.Response(body=b"<p>busy</p>", content_type="text/html")

    async def down(request: web.Request) -> web.StreamResponse:
        asked["down"] += 1
        if asked["down"] <= 2:
            return web.Response(status=503)
        return web.Response(body=b"<p>down</p>", content_type="text/html")

    resources: dict[str, Resource | Handler] = {"/": page("/busy", "/down"), "/busy": busy}
    resources["/down"] = down
    async with Site(resources) as site:
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url(), ignore_robots=True), sink, rng=Lowest())
    assert report.exit_code is ExitCode.OK
    assert report.stats.pages == 3
    assert report.stats.retries == {"HTTP 429": 1, "HTTP 503": 2}
    assert report.stats.statuses == {200: 3, 429: 1, 503: 2}
    throttled = next(r for r in site.requests if r.target == "/busy")
    later = [r for r in site.requests if r.at > throttled.at and r.target != "/down"]
    retries_of_down = [r for r in site.requests if r.target == "/down"][1:]
    assert all(r.at - throttled.at >= 1.0 - 0.05 for r in later + retries_of_down)


class Firewall:
    """A site of `pages` pages behind a firewall that refuses every request with 403
    Forbidden once the start page has been served, until it has refused `probes` requests
    for the start page itself, the crawl's probes. Then it serves the crawl again."""

    def __init__(self, pages: int, probes: int) -> None:
        self.pages = pages
        self.probes = probes
        self.refused_probes = 0
        self.started = False

    @property
    def resources(self) -> Mapping[str, Resource | Handler]:
        return {"/": self.handle} | {f"/{n}": self.handle for n in range(self.pages)}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if self.started and self.refused_probes < self.probes:
            self.refused_probes += request.path == "/"
            return web.Response(status=403, body=b"<p>Forbidden</p>", content_type="text/html")
        links = range(self.pages) if request.path == "/" else ()
        self.started = True
        markup = "".join(f'<a href="/{n}">{n}</a>' for n in links)
        return web.Response(body=markup.encode(), content_type="text/html")


async def test_a_run_of_403s_that_ends_loses_no_page(caplog: pytest.LogCaptureFixture) -> None:
    async with Site(Firewall(pages=40, probes=2).resources) as site:
        sink = MemorySink()
        cfg = Config(start_url=site.url(), ignore_robots=True, first_probe=0.05)
        report = await crawl(cfg, sink)
    assert report.exit_code is ExitCode.OK
    assert report.stats.pages == 41
    assert not report.stats.failures
    assert (
        f"20 requests in a row were refused with 403 Forbidden, as a block would refuse them: "
        f"pausing the crawl, and checking {site.url()} until it's served again" in caplog.text
    )
    assert f"{site.url()} is served again (HTTP 200): resuming the crawl" in caplog.text


async def test_a_run_of_403s_that_goes_on_aborts_with_exit_4(
    caplog: pytest.LogCaptureFixture,
) -> None:
    firewall = Firewall(pages=40, probes=1000)
    async with Site(firewall.resources) as site:
        sink = MemorySink()
        cfg = Config(start_url=site.url(), ignore_robots=True, first_probe=0.05, give_up_after=0.5)
        report = await crawl(cfg, sink)
    assert report.exit_code is ExitCode.ABORTED
    assert len(sink.records) == 1  # the start page, kept
    assert 2 <= firewall.refused_probes <= 5  # backing off from 0.05 s
    assert (
        f"aborting: {site.url()} still answers 403 Forbidden after 0.5 s, as a block would"
        in caplog.text
    )


async def test_a_host_that_goes_away_aborts_with_exit_4(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Once the start page is served, the server goes away: the requests in flight are cut
    # off, and each one after is refused. The breaker opens, the probes find no one, and
    # the crawl gives up, keeping what it printed.
    async def slow(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(10)
        return web.Response()

    resources: dict[str, Resource | Handler] = {"/": page(*(f"/{n}" for n in range(40)))}
    resources |= {f"/{n}": slow for n in range(40)}
    async with Site(resources) as site:
        cfg = Config(start_url=site.url(), ignore_robots=True, first_probe=0.05, give_up_after=1)
        sink = MemorySink()
        crawling = asyncio.create_task(crawl(cfg, sink, rng=Lowest()))
        await until(lambda: len(site.requests) > 1)  # the start page's links are wanted
        await site.close()
        started = time.monotonic()
        report = await crawling
    assert time.monotonic() - started < 10
    assert report.exit_code is ExitCode.ABORTED
    assert len(sink.records) == 1
    assert "requests in a row got no answer (connect): pausing the crawl" in caplog.text
    assert f"aborting: {site.url()} has given no answer for 1 s (connect)" in caplog.text


async def test_a_decompression_bomb_costs_no_more_than_the_cap(parse_workers: int) -> None:
    # 2 GiB of zeros after a link, as 2,048 gzip members of 1 MiB each, sent in 2 MB: only
    # --max-page-bytes of it is decompressed, and the page is printed with its link.
    member = gzip.compress(bytes(MIB))
    bomb = gzip.compress(b'<a href="/after">after</a>') + member * 2048
    resources = {
        "/": page("/bomb"),
        "/bomb": Resource(
            body=bomb, headers=(("Content-Type", "text/html"), ("Content-Encoding", "gzip"))
        ),
        "/after": page(),
    }
    async with Site(resources) as site:
        sink = MemorySink()
        cfg = Config(
            start_url=site.url(),
            ignore_robots=True,
            max_page_bytes=4 * MIB,
            parse_workers=parse_workers,
        )
        report = await crawl(cfg, sink)
    assert report.exit_code is ExitCode.OK
    assert f"{site.url('/bomb')}\n  {site.url('/after')}\n\n".encode() in sink.records
    assert report.stats.truncated == 1
    assert 4 * MIB <= report.stats.decoded_bytes < 4 * MIB + 1000  # and two small pages


def _big_site() -> dict[str, Resource | Handler]:
    """300 pages, each linking to all of them: far more output than a pipe holds."""
    links = [f"/{n}" for n in range(1, 300)]
    resources: dict[str, Resource | Handler] = {f"/{n}": page(*links) for n in range(1, 300)}
    return resources | {"/": page(*links)}


@pytest.mark.parametrize("stderr", ["", " 2>&1"], ids=["stdout", "stdout and stderr"])
async def test_a_closed_stdout_ends_the_crawl_quietly_with_141(
    stderr: str, parse_workers: int
) -> None:
    # As `crawl URL | head` does: head reads three lines and exits, and the crawler's next
    # write finds no reader. bash exits with the crawler's status.
    async with Site(_big_site()) as site:
        command = (
            f"{CRAWL} --ignore-robots --parse-workers {parse_workers} {site.url()}{stderr} "
            '| head -n 3; exit "${PIPESTATUS[0]}"'
        )
        code, stdout, error = await run("bash", "-c", command)
    assert code == ExitCode.STDOUT_CLOSED
    assert stdout.decode().splitlines() == [
        site.url(),
        f"  {site.url('/1')}",
        f"  {site.url('/2')}",
    ]
    if stderr:  # stderr went to head as well, which printed none of it
        assert error == ""
    else:  # stderr is still there, for the summary, and for nothing else
        assert error.split(" pages in ")[0].isdigit()
        assert "Traceback" not in error
