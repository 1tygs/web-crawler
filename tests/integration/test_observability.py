"""What the crawl command says on stderr as it goes (DESIGN.md §10): -v, -vv and -q, and the
progress line on a terminal."""

import asyncio
import fcntl
import os
import pty
import struct
import termios
from pathlib import Path

from aiohttp import web

from tests.support.command import CRAWL, run
from tests.support.site import Handler, Resource, Site, page, unused_port
from web_crawler.cli import TIP, _ProgressLine
from web_crawler.types import ExitCode

ROBOTS = Resource(
    body=b"User-agent: *\nDisallow: /private\n", headers=(("Content-Type", "text/plain"),)
)


def small_site() -> dict[str, Resource | Handler]:
    """A start page linking to ten pages, a missing one and four that robots.txt disallows:
    enough responses for the concurrency law's first round."""
    links = [*(f"/{n}" for n in range(10)), "/missing", *(f"/private/{n}" for n in range(4))]
    pages: dict[str, Resource | Handler] = {f"/{n}": page() for n in range(10)}
    return pages | {"/": page(*links), "/robots.txt": ROBOTS}


def logged(stderr: str) -> list[str]:
    return [
        line.removeprefix("crawl: ") for line in stderr.splitlines() if line.startswith("crawl: ")
    ]


async def test_v_logs_failures_the_first_urls_skipped_and_the_limits_decisions() -> None:
    async with Site(small_site()) as site:
        code, _, stderr = await run(CRAWL, "-v", "--parse-workers", "0", site.url())
    assert code == 0
    lines = logged(stderr)
    assert f"{site.url('/missing')} isn't printed: HTTP 404" in lines
    assert [line for line in lines if line.startswith("not crawling ")] == [
        f"not crawling {site.url(f'/private/{n}')}: robots.txt" for n in range(3)
    ]
    assert any(line.startswith("concurrency limit: base: 8 -> 8, median ") for line in lines)
    assert not any(": HTTP 200, " in line for line in lines)  # requests are -vv's
    assert stderr.splitlines()[len(lines)].startswith("11 pages in ")  # then the summary


async def test_vv_logs_every_request_and_every_url_skipped() -> None:
    async with Site(small_site()) as site:
        code, _, stderr = await run(CRAWL, "-vv", "--parse-workers", "0", site.url())
    assert code == 0
    lines = logged(stderr)
    assert f"{site.url('/robots.txt')}: HTTP 200" in lines
    requests = [line for line in lines if ": HTTP " in line and "ms to first byte" in line]
    assert len(requests) == 12  # the start page, ten pages and the missing one
    assert f"not crawling {site.url('/private/3')}: robots.txt" in lines


async def test_q_prints_errors_only() -> None:
    async with Site(small_site()) as site:
        code, stdout, stderr = await run(CRAWL, "-q", "--parse-workers", "0", site.url())
    assert code == 0
    assert stdout.count(b"\n\n") == 11
    assert stderr == ""
    port = unused_port()
    code, _, stderr = await run(CRAWL, "-q", f"http://127.0.0.1:{port}/")
    assert code == ExitCode.CANNOT_START
    assert stderr.startswith("crawl: can't start: ")


async def _slow(request: web.Request) -> web.StreamResponse:
    await asyncio.sleep(0.4)
    return web.Response(body=b"<!doctype html>slow", content_type="text/html")


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:  # Linux: the terminal's other end is closed
            break
        if not chunk:  # macOS
            break
        chunks.append(chunk)
    return b"".join(chunks)


async def test_a_terminal_shows_the_progress_line_and_the_summary_after_it(
    tmp_path: Path,
) -> None:
    # stderr is a terminal 60 columns wide, and the records go to a file.
    links = [f"/{n}" for n in range(20)]
    resources: dict[str, Resource | Handler] = {f"/{n}": _slow for n in range(20)}
    resources["/"] = page(*links)
    main, terminal = pty.openpty()
    fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 60, 0, 0))
    async with Site(resources) as site:
        process = await asyncio.create_subprocess_exec(
            CRAWL, "--ignore-robots", "-o", str(tmp_path / "out"), site.url(), stderr=terminal
        )
        os.close(terminal)
        shown, code = await asyncio.gather(asyncio.to_thread(_read_all, main), process.wait())
    os.close(main)
    assert code == 0
    text = shown.decode()
    # Redrawn in place as the crawl goes, each time cut to the terminal's width; then taken
    # away, and the summary printed. The terminal ends lines with CR LF.
    progress, summary = text.rsplit("\r\x1b[K", 1)
    frames = progress.split("\r")
    assert frames[0] == ""
    assert len(frames[1:]) >= 3
    for frame in frames[1:]:
        assert frame.startswith("pages ")
        assert frame.endswith("\x1b[K")
        assert len(frame.removesuffix("\x1b[K")) == 59  # every line is longer than that
    assert summary.startswith("21 pages in ")
    assert summary.endswith("parser processes aside\r\n")
    assert TIP not in text


def test_a_terminal_that_doesnt_say_how_wide_it_is_has_80_columns() -> None:
    main, terminal = pty.openpty()  # 0 by 0, as a new terminal says it is
    with os.fdopen(terminal, "w") as stream:
        _ProgressLine(stream).show("x" * 100)
        shown = os.read(main, 1024)
    os.close(main)
    assert shown == b"\r" + b"x" * 79 + b"\x1b[K"
