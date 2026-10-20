"""Whole crawls of in-process sites over real sockets, checked against ground truth."""

import asyncio
import signal
import sys
from collections import Counter
from pathlib import Path

import pytest
from aiohttp import web

from tests.support.bench import make_site
from tests.support.graphs import expected_records, graph_site
from tests.support.site import Handler, Resource, Site, page, redirect, unused_port
from web_crawler.config import Config
from web_crawler.engine import crawl
from web_crawler.output import MemorySink
from web_crawler.types import ExitCode

# The console script that `uv sync` installs next to the interpreter.
CRAWL = str(Path(sys.executable).with_name("crawl"))


def split_records(stdout: bytes) -> list[bytes]:
    """stdout as whole records, sorted. Each record ends with a blank line, and no line
    is blank otherwise."""
    *records, rest = stdout.split(b"\n\n")
    assert rest == b"", "the output ends inside a record"
    return sorted(record + b"\n\n" for record in records)


async def run(*args: str) -> tuple[int, bytes, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    assert process.returncode is not None
    return process.returncode, stdout, stderr.decode()


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


async def test_a_hand_built_site() -> None:
    other_port = unused_port()
    async with Site({}) as site:
        site.resources = hand_built(site.port, other_port)
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url()), sink)
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


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("max_concurrency", [1, 64])
async def test_random_sites_match_breadth_first_ground_truth(
    seed: int, max_concurrency: int
) -> None:
    graph = make_site(seed, pages=300, branching=3.0, cross_links=2)
    async with Site({}) as site:
        site.resources = graph_site(site.origin, graph)
        sink = MemorySink()
        report = await crawl(Config(start_url=site.url(), max_concurrency=max_concurrency), sink)
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


async def test_follows_a_cookie_gate_back_to_the_page() -> None:
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
        report = await crawl(Config(start_url=site.url()), sink)
    assert report.exit_code is ExitCode.OK
    o = site.origin
    assert sorted(sink.records) == [
        f"{o}/\n  {o}/members\n\n".encode(),
        f"{o}/members\n  {o}/\n\n".encode(),
    ]
    assert site.targets() == ["/robots.txt", "/", "/members", "/gate?next=/members", "/members"]
    assert not report.stats.failures


async def test_a_redirect_loop_ends_at_the_hop_limit() -> None:
    resources = {"/": page("/loop"), "/loop": redirect("/loop/"), "/loop/": redirect("/loop")}
    async with Site(resources) as site:
        report = await crawl(Config(start_url=site.url()), MemorySink())
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
        "{start}: HTTP 301 to http://localhost:{port}/, outside the crawl's scope",
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


async def test_ctrl_c_exits_130_with_whole_records() -> None:
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
        process.send_signal(signal.SIGINT)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == ExitCode.INTERRUPTED
    assert split_records(stdout) == [
        b"http://127.0.0.1:%d/\n" % site.port
        + b"".join(b"  http://127.0.0.1:%d/%d\n" % (site.port, n) for n in range(20))
        + b"\n"
    ]
    assert b"Traceback" not in stderr
