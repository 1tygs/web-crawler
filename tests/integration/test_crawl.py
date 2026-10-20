"""Whole crawls of in-process sites over real sockets, checked against ground truth."""

import asyncio
import gzip
import os
import signal
import sys
import time
import zlib
from collections import Counter
from collections.abc import Callable
from compression import zstd
from pathlib import Path

import brotli
import pytest
from aiohttp import web

from tests.support.bench import make_site
from tests.support.command import CRAWL, run, split_records
from tests.support.graphs import expected_records, graph_site, href
from tests.support.site import HTML, Handler, Resource, Site, page, redirect, unused_port
from tests.support.waiting import until
from tests.support.workers import scripted
from web_crawler.config import Config
from web_crawler.engine import crawl
from web_crawler.output import MemorySink
from web_crawler.pool import Parser, ProcessParser
from web_crawler.types import ExitCode, WorkerContext


def hand_built(port: int, other_port: int) -> dict[str, Resource]:
    return {
        "/": page(
            "/a",
            "b",
            "/c?x=%2f",
            "https://external.example/",
            f"http://localhost:{port}/",  # the same server, by another name: another host
            f"http://127.0.0.1:{other_port}/",
            "/old",
            "/also-old",  # redirects to /new too: /new is fetched and printed once
            "/file.bin",
            "/missing",
            "#top",
            "mailto:someone@example.com",
            "/a#section",
            "/~user/",
            "/%7Euser/",  # the same resource: printed as written, requested once
            "/private/page",  # disallowed by robots.txt: printed, never requested
        ),
        "/robots.txt": Resource(
            body=b"User-agent: *\nDisallow: /private/\n",
            headers=(("Content-Type", "text/plain"),),
        ),
        "/a": page("/", "/a/deeper/", "../c?x=%2f"),
        "/b": page("/a"),
        "/c?x=%2f": page("/"),
        "/a/deeper/": page("../..", "/cycle"),
        "/cycle": page("/a/deeper/"),
        "/old": redirect("/new"),
        "/also-old": redirect("/new", status=302),
        "/new": page("/"),
        "/file.bin": Resource(
            body=b"\0" * 100, headers=(("Content-Type", "application/octet-stream"),)
        ),
        "/~user/": page("/%7Euser/"),
    }


def hand_built_records(port: int, other_port: int) -> list[bytes]:
    o = f"http://127.0.0.1:{port}"
    pages = {
        f"{o}/": [
            f"{o}/a",
            f"{o}/b",
            f"{o}/c?x=%2f",
            "https://external.example/",
            f"http://localhost:{port}/",
            f"http://127.0.0.1:{other_port}/",
            f"{o}/old",
            f"{o}/also-old",
            f"{o}/file.bin",
            f"{o}/missing",
            f"{o}/",
            f"{o}/~user/",
            f"{o}/%7Euser/",
            f"{o}/private/page",
        ],
        f"{o}/a": [f"{o}/", f"{o}/a/deeper/", f"{o}/c?x=%2f"],
        f"{o}/b": [f"{o}/a"],
        f"{o}/c?x=%2f": [f"{o}/"],
        f"{o}/a/deeper/": [f"{o}/", f"{o}/cycle"],
        f"{o}/cycle": [f"{o}/a/deeper/"],
        f"{o}/new": [f"{o}/"],  # printed under the URL the redirect led to
        f"{o}/~user/": [f"{o}/%7Euser/"],
    }
    return sorted(
        "".join([url, "\n", *(f"  {link}\n" for link in links), "\n"]).encode()
        for url, links in pages.items()
    )


async def test_a_hand_built_site(parse_workers: int) -> None:
    other_port = unused_port()
    async with Site({}) as site:
        site.resources = hand_built(site.port, other_port)
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url(), parse_workers=parse_workers), sink)
    assert report.exit_code is ExitCode.OK
    assert sorted(sink.records) == hand_built_records(site.port, other_port)
    # Each in-scope URL was requested once, exactly as printed, and nothing else was.
    assert sorted(site.targets()) == sorted(
        [
            "/robots.txt",
            "/",
            "/a",
            "/b",
            "/c?x=%2f",
            "/a/deeper/",
            "/cycle",
            "/old",
            "/also-old",
            "/new",
            "/file.bin",
            "/missing",
            "/~user/",
        ]
    )
    assert {request.headers["Host"] for request in site.requests} == {f"127.0.0.1:{site.port}"}
    stats = report.stats
    assert (stats.pages, stats.not_html) == (8, 1)
    # Whichever of /old and /also-old answered second found /new already claimed.
    assert stats.failures == {"HTTP 404": 1, "redirect to a URL already claimed": 1}
    assert stats.statuses == {200: 9, 301: 1, 302: 1, 404: 1}
    assert report.unique_urls == 13  # /new, reached only by redirect, among them
    assert report.rejected == {"robots.txt": 1}


def _compressed(name: str, *codings: str, content_type: str | None = HTML) -> Resource:
    """A page that links to /NAME/next, with codings applied in order."""
    body = f'<!doctype html><a href="/{name}/next">next</a>'.encode()
    for coding in codings:
        body = COMPRESS[coding](body)
    headers = [("Content-Type", content_type)] if content_type else []
    headers += [("Content-Encoding", coding) for coding in codings]
    return Resource(body=body, headers=tuple(headers))


COMPRESS: dict[str, Callable[[bytes], bytes]] = {
    "gzip": gzip.compress,
    "x-gzip": gzip.compress,
    "deflate": zlib.compress,
    "br": brotli.compress,
    "zstd": zstd.compress,
    "utf-8": lambda body: body,  # a coding no server should send: not one at all
}


def _compressed_site() -> dict[str, Resource]:
    pages = {
        "gzip": _compressed("gzip", "gzip"),
        "x-gzip": _compressed("x-gzip", "x-gzip"),
        "deflate": _compressed("deflate", "deflate"),
        "br": _compressed("br", "br"),
        "zstd": _compressed("zstd", "zstd"),
        "stacked": _compressed("stacked", "gzip", "br"),  # two Content-Encoding fields
        "utf-8": _compressed("utf-8", "utf-8"),
        "sniffed": _compressed("sniffed", "gzip", content_type=None),
    }
    resources = {f"/{name}": page for name, page in pages.items()}
    resources |= {f"/{name}/next": page() for name in pages}
    raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    body = b'<a href="/raw-deflate/next">next</a>'
    resources["/raw-deflate"] = Resource(
        body=raw.compress(body) + raw.flush(),
        headers=(("Content-Type", HTML), ("Content-Encoding", "deflate")),
    )
    resources["/raw-deflate/next"] = page()
    # 64 MiB of zeros after a link, in 64 KiB: the crawl decompresses only up to the cap.
    bomb = b'<a href="/bomb/next">next</a>' + bytes(64 << 20)
    resources["/bomb"] = Resource(
        body=gzip.compress(bomb), headers=(("Content-Type", HTML), ("Content-Encoding", "gzip"))
    )
    resources["/bomb/next"] = page()
    resources["/corrupt"] = Resource(
        body=b"<a href=/corrupt/next>", headers=(("Content-Type", HTML), ("Content-Encoding", "br"))
    )
    resources["/nosniff"] = Resource(
        body=b"<a href=/nosniff/next>", headers=(("X-Content-Type-Options", "nosniff"),)
    )
    names = [*pages, "raw-deflate", "bomb", "corrupt", "nosniff"]
    resources["/"] = page(*(f"/{name}" for name in names))
    return resources


async def test_compressed_pages_are_crawled_as_a_browser_reads_them(parse_workers: int) -> None:
    async with Site(_compressed_site()) as site:
        sink = MemorySink()
        cfg = Config(start_url=site.url(), max_page_bytes=1 << 20, parse_workers=parse_workers)
        report = await crawl(cfg, sink)
    assert report.exit_code is ExitCode.OK
    o = site.origin
    names = ["gzip", "x-gzip", "deflate", "br", "zstd", "stacked", "utf-8", "sniffed"]
    names += ["raw-deflate", "bomb"]
    assert sorted(sink.records) == sorted(
        [
            b"".join(
                [f"{o}/\n".encode()]
                + [f"  {o}/{name}\n".encode() for name in [*names, "corrupt", "nosniff"]]
                + [b"\n"]
            ),
            *(f"{o}/{name}\n  {o}/{name}/next\n\n".encode() for name in names),
            *(f"{o}/{name}/next\n\n".encode() for name in names),
        ]
    )
    assert report.stats.failures == {"bad content-encoding": 1}
    assert report.stats.not_html == 1  # nosniff: its body isn't sniffed
    assert report.stats.decoded_bytes < 2 << 20  # the bomb stopped at the cap


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("max_concurrency", [1, 64])
async def test_random_sites_match_breadth_first_ground_truth(
    seed: int, max_concurrency: int, parse_workers: int
) -> None:
    graph = make_site(seed, pages=300, branching=3.0, cross_links=2)
    async with Site({}) as site:
        site.resources = graph_site(site.origin, graph)
        sink = MemorySink()
        cfg = Config(
            start_url=site.url(), max_concurrency=max_concurrency, parse_workers=parse_workers
        )
        report = await crawl(cfg, sink)
    assert report.exit_code is ExitCode.OK
    assert sorted(sink.records) == expected_records(site.origin, graph)
    assert max(Counter(site.targets()).values()) == 1  # no URL requested twice


async def test_the_crawl_command_prints_the_ground_truth() -> None:
    graph = make_site(7, pages=200, branching=3.0, cross_links=2)
    async with Site({}) as site:
        site.resources = graph_site(site.origin, graph)
        code, stdout, stderr = await run(CRAWL, site.url())
    assert code == 0
    assert split_records(stdout) == expected_records(site.origin, graph)
    assert stderr.startswith(f"{len(graph)} pages in ")
    assert "Traceback" not in stderr


async def test_python_dash_m_crawls_the_hand_built_site() -> None:
    other_port = unused_port()
    async with Site({}) as site:
        site.resources = hand_built(site.port, other_port)
        code, stdout, stderr = await run(sys.executable, "-m", "web_crawler", site.url())
    assert code == 0
    assert split_records(stdout) == hand_built_records(site.port, other_port)
    assert "statuses: 200 (9), 301 (1), 302 (1), 404 (1)" in stderr
    assert (
        "not printed: not HTML (1), HTTP 404 (1), redirect to a URL already claimed (1)" in stderr
    )


async def test_follows_a_cookie_gate_back_to_the_page(parse_workers: int) -> None:
    # /members redirects to a page that sets a cookie and redirects back, as many sites'
    # consent and login gates do. With the cookie, /members is served.
    async def members(request: web.Request) -> web.StreamResponse:
        if request.cookies.get("gate") == "open":
            return web.Response(body=b'<a href="/">home</a>', content_type="text/html")
        return web.Response(status=302, headers={"Location": "/gate?next=/members"})

    async def gate(request: web.Request) -> web.StreamResponse:
        response = web.Response(status=302, headers={"Location": request.query["next"]})
        response.set_cookie("gate", "open")
        return response

    resources: dict[str, Resource | Handler] = {
        "/": page("/members"),
        "/members": members,
        "/gate?next=/members": gate,
    }
    async with Site(resources) as site:
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url(), parse_workers=parse_workers), sink)
    assert report.exit_code is ExitCode.OK
    o = site.origin
    assert sorted(sink.records) == [
        f"{o}/\n  {o}/members\n\n".encode(),
        f"{o}/members\n  {o}/\n\n".encode(),
    ]
    assert site.targets() == ["/robots.txt", "/", "/members", "/gate?next=/members", "/members"]
    assert not report.stats.failures


async def test_a_redirect_loop_ends_at_the_hop_limit(parse_workers: int) -> None:
    resources = {"/": page("/loop"), "/loop": redirect("/loop/"), "/loop/": redirect("/loop")}
    async with Site(resources) as site:
        cfg = Config(start_url=site.url(), parse_workers=parse_workers)
        report = await crawl(cfg, MemorySink())
    assert report.stats.failures == {"redirect loop": 1}
    assert site.targets() == ["/robots.txt", "/", *(["/loop", "/loop/"] * 6)[:11]]


START_FAILURES = {
    "error status": (Resource(status=500), "{start}: HTTP 500"),
    "not html": (
        Resource(body=b"%PDF", headers=(("Content-Type", "application/pdf"),)),
        "{start} isn't an HTML page (Content-Type: application/pdf)",
    ),
    "redirect to another host": (
        redirect("http://localhost:{port}/"),
        "{start} redirects to http://localhost:{port}/, outside the crawl's scope; to crawl "
        "there instead, run: crawl http://localhost:{port}/",
    ),
}


@pytest.mark.parametrize("case", START_FAILURES)
async def test_a_start_url_that_isnt_a_page_exits_3(case: str) -> None:
    resource, message = START_FAILURES[case]
    async with Site({}) as site:
        headers = tuple((name, value.format(port=site.port)) for name, value in resource.headers)
        site.resources = {"/": Resource(resource.status, resource.body, headers)}
        code, stdout, stderr = await run(CRAWL, site.url())
    assert code == ExitCode.CANNOT_START
    assert stdout == b""
    reason = message.format(start=site.url(), port=site.port)
    assert stderr == f"crawl: can't start: {reason}\n"  # and no summary


async def test_an_unreachable_start_url_exits_3() -> None:
    port = unused_port()
    code, stdout, stderr = await run(CRAWL, f"http://127.0.0.1:{port}/")
    assert code == ExitCode.CANNOT_START
    assert stdout == b""
    assert stderr.startswith(
        f"crawl: can't start: http://127.0.0.1:{port}/robots.txt got no answer (connect: "
    )
    assert stderr.endswith(", 3 tries)\n")
    assert "--ignore-robots" not in stderr


DISALLOW_ALL = Resource(
    body=b"User-agent: *\nDisallow: /\n", headers=(("Content-Type", "text/plain"),)
)


async def test_a_start_url_robots_txt_disallows_exits_3() -> None:
    async with Site({"/robots.txt": DISALLOW_ALL, "/": page("/a")}) as site:
        code, stdout, stderr = await run(CRAWL, site.url())
    assert code == ExitCode.CANNOT_START
    assert stdout == b""
    assert stderr == (
        f"crawl: can't start: {site.url('/robots.txt')} disallows {site.url()} for "
        "web-crawler; if the site is yours, --ignore-robots skips robots.txt\n"
    )
    assert site.targets() == ["/robots.txt"]


async def test_ignore_robots_crawls_without_reading_robots_txt() -> None:
    resources = {"/robots.txt": DISALLOW_ALL, "/": page("/a"), "/a": page()}
    async with Site(resources) as site:
        code, stdout, _ = await run(CRAWL, "--ignore-robots", site.url())
    assert code == 0
    assert len(split_records(stdout)) == 2
    assert site.targets() == ["/", "/a"]


async def test_a_usage_error_exits_2() -> None:
    code, stdout, stderr = await run(CRAWL, "ftp://example.com/")
    assert code == ExitCode.USAGE
    assert stdout == b""
    assert "crawl: error: 'ftp://example.com/' isn't an http or https URL" in stderr


# A job that a non-interactive shell starts in the background has SIGINT ignored, and its
# children inherit that; the crawler honours it, as nohup relies on. So the crawler under
# test starts through this, which restores the default whatever the test runner inherited.
WITH_SIGINT = (
    sys.executable,
    "-c",
    "import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); "
    "os.execv(sys.argv[1], sys.argv[1:])",
)


@pytest.mark.parametrize(
    ("sig", "code"),
    [(signal.SIGINT, ExitCode.INTERRUPTED), (signal.SIGTERM, ExitCode.TERMINATED)],
)
async def test_a_signal_stops_the_crawl_with_whole_records(
    sig: signal.Signals, code: ExitCode
) -> None:
    started = asyncio.Event()

    async def slow(request: web.Request) -> web.StreamResponse:
        started.set()
        await asyncio.sleep(30)
        return web.Response()

    resources = {"/": page(*(f"/{n}" for n in range(20)))} | {f"/{n}": slow for n in range(20)}
    async with Site(resources) as site:
        process = await asyncio.create_subprocess_exec(
            *WITH_SIGINT,
            CRAWL,
            site.url(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(started.wait(), 10)  # the start page is done; fetches wait
        process.send_signal(sig)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == code
    assert split_records(stdout) == [
        b"http://127.0.0.1:%d/\n" % site.port
        + b"".join(b"  http://127.0.0.1:%d/%d\n" % (site.port, n) for n in range(20))
        + b"\n"
    ]
    assert b"Traceback" not in stderr
    assert "1 page in" in stderr.decode()  # the summary


class _InterruptedWhileClosing(MemorySink):
    """Gets a second Ctrl-C while it closes."""

    def close(self) -> None:
        os.kill(os.getpid(), signal.SIGINT)
        super().close()


def _slow_after(sig: signal.Signals, times: int = 1) -> dict[str, Resource | Handler]:
    """A site whose start page links to a page that sends sig to the crawler, in-process."""

    async def slow(request: web.Request) -> web.StreamResponse:
        for _ in range(times):
            os.kill(os.getpid(), sig)
        await asyncio.sleep(30)
        return web.Response()

    return {"/": page("/slow"), "/slow": slow}


async def test_a_second_ctrl_c_exits_at_once(
    monkeypatch: pytest.MonkeyPatch, parse_workers: int
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)
    before = signal.getsignal(signal.SIGINT)
    async with Site(_slow_after(signal.SIGINT)) as site:
        sink = _InterruptedWhileClosing()
        cfg = Config(start_url=site.url(), ignore_robots=True, parse_workers=parse_workers)
        report = await crawl(cfg, sink)
    assert report.exit_code is ExitCode.INTERRUPTED
    assert exits == [ExitCode.INTERRUPTED]
    assert sink.records == [b"%s\n  %s\n\n" % (site.url().encode(), site.url("/slow").encode())]
    assert signal.getsignal(signal.SIGINT) is before


async def test_a_second_ctrl_c_during_start_up_exits_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)

    async def robots(request: web.Request) -> web.StreamResponse:
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(30)
        return web.Response()

    async with Site({"/robots.txt": robots, "/": page()}) as site:
        sink = _InterruptedWhileClosing()
        report = await crawl(Config(start_url=site.url()), sink)
    assert report.exit_code is ExitCode.INTERRUPTED
    assert exits == [ExitCode.INTERRUPTED]  # with no parser processes started to kill
    assert sink.records == []


async def test_sigterm_stops_the_crawl_and_an_ignored_sigint_stays_ignored(
    parse_workers: int,
) -> None:
    before = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        async with Site(_slow_after(signal.SIGTERM, times=2)) as site:
            sink = MemorySink()
            cfg = Config(start_url=site.url(), ignore_robots=True, parse_workers=parse_workers)
            report = await crawl(cfg, sink)
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, before)
    assert report.exit_code is ExitCode.TERMINATED
    assert sink.closed
    assert len(sink.records) == 1
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def _wide_site(size: int) -> dict[str, Resource | Handler]:
    """size pages, each linking to all of them, and to a missing page and a non-page."""
    links = ["/missing", "/file.bin", *(f"/{n}" for n in range(1, size))]
    pages: dict[str, Resource | Handler] = {f"/{n}": page(*links) for n in range(1, size)}
    return pages | {
        "/": page(*links),
        "/file.bin": Resource(body=b"\0", headers=(("Content-Type", "application/zip"),)),
    }


@pytest.mark.parametrize(("max_pages", "printed"), [(1, 1), (3, 3), (20, 20), (100, 30)])
async def test_max_pages_prints_and_fetches_at_most_n_pages(
    max_pages: int, printed: int, parse_workers: int
) -> None:
    async with Site(_wide_site(30)) as site:
        sink = MemorySink()
        cfg = Config(
            start_url=site.url(),
            max_pages=max_pages,
            ignore_robots=True,
            parse_workers=parse_workers,
        )
        report = await crawl(cfg, sink)
    assert report.exit_code is ExitCode.OK
    assert len(sink.records) == printed  # exactly N, or every page of a smaller site
    page_fetches = [t for t in site.targets() if t not in ("/missing", "/file.bin")]
    assert len(page_fetches) == printed  # no page fetched only to be thrown away
    assert len(set(page_fetches)) == printed


def _scripted_pool(ctx: WorkerContext) -> Parser:
    """Two parser processes whose parses misbehave as their pages' URLs say."""
    return ProcessParser(ctx, 2, target=scripted)


async def _hang(request: web.Request) -> web.StreamResponse:
    await asyncio.sleep(30)
    return web.Response()


async def test_the_first_ctrl_c_lets_parses_already_submitted_finish(tmp_path: Path) -> None:
    marker = tmp_path / "parsing"
    slow = f"/slow?sleep=0.5&marker={marker}"
    resources: dict[str, Resource | Handler] = {
        "/": page(slow, "/hanging"),
        slow: page("/next"),
        "/hanging": _hang,
    }
    async with Site(resources) as site:

        async def interrupt() -> None:
            await until(marker.exists)  # the slow page is submitted, /hanging still fetching
            os.kill(os.getpid(), signal.SIGINT)

        interrupter = asyncio.create_task(interrupt())
        sink = MemorySink()
        cfg = Config(start_url=site.url(), ignore_robots=True)
        report = await crawl(cfg, sink, parser=_scripted_pool)
        await interrupter
    assert report.exit_code is ExitCode.INTERRUPTED
    assert sorted(sink.records) == [
        f"{site.url()}\n  {site.url(slow)}\n  {site.url('/hanging')}\n\n".encode(),
        f"{site.url(slow)}\n  {site.url('/next')}\n\n".encode(),
    ]
    assert "/next" not in site.targets()  # nothing new was fetched


async def test_a_second_ctrl_c_kills_busy_parser_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)  # so that the test carries on
    marker = tmp_path / "parsing"
    slow = f"/slow?sleep=60&marker={marker}"
    async with Site({"/": page(slow), slow: page()}) as site:

        async def interrupt_twice() -> None:
            await until(marker.exists)
            os.kill(os.getpid(), signal.SIGINT)  # waits for the slow parse
            await asyncio.sleep(0.2)
            os.kill(os.getpid(), signal.SIGINT)

        interrupter = asyncio.create_task(interrupt_twice())
        started = time.monotonic()
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url()), sink, parser=_scripted_pool)
        await interrupter
    assert exits == [ExitCode.INTERRUPTED]
    assert time.monotonic() - started < 30  # the parse didn't run its 60 s
    assert report.exit_code is ExitCode.INTERRUPTED
    assert len(sink.records) == 1  # the start page's
    # With os._exit stubbed out, the killed parse fails as a pool broken for good would.
    assert "BrokenProcessPool" in caplog.text


async def test_a_parser_process_killed_mid_parse_loses_no_page(tmp_path: Path) -> None:
    # The first parse of the page dies with its process, as on a kill -9.
    victim = f"/victim?kill=once&marker={tmp_path / 'killed'}"
    graph = make_site(3, pages=60, branching=3.0, cross_links=2)
    async with Site({}) as site:
        home = page(victim, *(href(site.origin, 0, target) for target in graph[0]))
        site.resources = graph_site(site.origin, graph) | {"/": home, victim: page("/")}
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url()), sink, parser=_scripted_pool)
    assert (tmp_path / "killed").exists()
    assert report.exit_code is ExitCode.OK
    assert not report.stats.failures
    assert len(sink.records) == len(graph) + 1  # every page, the victim too


async def test_a_page_that_crashes_its_parser_fails_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    crash = "/crash?kill=always"
    resources = {"/": page(crash, "/fine"), crash: page("/unseen"), "/fine": page()}
    async with Site(resources) as site:
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url()), sink, parser=_scripted_pool)
    assert report.exit_code is ExitCode.OK
    assert report.stats.failures == {"parse crashed": 1}
    assert sorted(sink.records) == [
        f"{site.url()}\n  {site.url(crash)}\n  {site.url('/fine')}\n\n".encode(),
        f"{site.url('/fine')}\n\n".encode(),
    ]
    assert f"{site.url(crash)} isn't printed: parsing it crashed the parser process" in caplog.text
    assert "not printed: parse crashed (1)" in report.summary
