"""The parse gateway's process pool: real worker processes, killed and crashed on purpose
(DESIGN.md §4.3, §5.4, A.9)."""

import asyncio
import json
import os
import subprocess
import sys
import time
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from tests.support.waiting import until
from tests.support.workers import scripted, worker_pid
from web_crawler.pool import InlineParser, ParseCrashed, ProcessParser
from web_crawler.types import ParseJob, ParseResult, WorkerContext

CTX = WorkerContext(("https://example.com/",), max_bytes=1 << 20)


def job(target: str) -> ParseJob:
    return ParseJob(f"https://example.com{target}", b'<a href="/next">next</a><a href="/x">x</a>')


async def inline(*jobs: ParseJob) -> list[ParseResult]:
    parser = InlineParser(CTX)
    return [await parser.parse(job) for job in jobs]


async def test_parses_as_the_inline_parser_does() -> None:
    parser = ProcessParser(CTX, 2)
    jobs = [job(f"/{n}") for n in range(20)]
    try:
        results = await asyncio.gather(*map(parser.parse, jobs))
    finally:
        await parser.close()
    assert results == await inline(*jobs)


async def test_starts_a_worker_only_when_none_is_idle(tmp_path: Path) -> None:
    parser = ProcessParser(CTX, 4, target=worker_pid)
    try:
        # One job at a time: the first worker takes each.
        pids = {(await parser.parse(job("/"))).record for _ in range(4)}
        assert len(pids) == 1
        # Four at once, which wait for each other: they finish only in four workers.
        together = f"rendezvous={tmp_path}&count=4"
        busy = await asyncio.gather(*(parser.parse(job(f"/{n}?{together}")) for n in range(4)))
        assert len({result.record for result in busy}) == 4
    finally:
        await parser.close()


async def test_a_worker_killed_mid_parse_loses_no_page(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # One worker is killed while the others are busy: every job in the pool fails with it,
    # and is submitted again to a new pool.
    marker = tmp_path / "killed"
    jobs = [job(f"/busy/{n}?sleep=0.3") for n in range(6)]
    jobs.append(job(f"/victim?kill=once&marker={marker}"))
    parser = ProcessParser(CTX, 4, target=scripted)
    try:
        results = await asyncio.gather(*map(parser.parse, jobs))
    finally:
        await parser.close()
    assert marker.exists()  # it was killed
    assert results == await inline(*jobs)
    assert caplog.text.count("a parser process died; starting its pool again") == 1


async def test_a_page_that_crashes_its_worker_fails_alone(caplog: pytest.LogCaptureFixture) -> None:
    jobs = [job(f"/busy/{n}?sleep=0.2") for n in range(5)]
    crasher = job("/crash?kill=always")
    parser = ProcessParser(CTX, 4, target=scripted)
    try:
        *results, crashed = await asyncio.gather(
            *map(parser.parse, [*jobs, crasher]), return_exceptions=True
        )
    finally:
        await parser.close()
    assert isinstance(crashed, ParseCrashed)
    assert str(crashed) == crasher.url
    assert results == await inline(*jobs)
    # It broke the pool twice, then the pool of one it ran alone in.
    assert caplog.text.count("a parser process died; starting its pool again") == 2


async def test_suspects_are_parsed_one_at_a_time() -> None:
    parser = ProcessParser(CTX, 2, target=worker_pid)
    try:
        started = time.monotonic()
        results = await asyncio.gather(
            *(parser._parse_alone(job(f"/{n}?sleep=0.2")) for n in range(3))
        )
        assert time.monotonic() - started >= 0.6
        assert len({result.record for result in results}) == 1  # in one worker of their own
    finally:
        await parser.close()


async def test_close_waits_for_the_parses_submitted() -> None:
    parser = ProcessParser(CTX, 1, target=scripted)
    parse = asyncio.create_task(parser.parse(job("/slow?sleep=0.5")))
    await asyncio.sleep(0)  # submitted
    started = time.monotonic()
    await parser.close()
    assert time.monotonic() - started >= 0.3
    assert await parse == (await inline(job("/slow?sleep=0.5")))[0]


async def test_kill_stops_a_busy_worker_at_once(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    parser = ProcessParser(CTX, 2, target=scripted)
    parse = asyncio.create_task(parser.parse(job(f"/slow?sleep=60&marker={marker}")))
    await until(marker.exists)
    started = time.monotonic()
    parser.kill()
    with pytest.raises(BrokenProcessPool):  # not submitted again: the crawl is exiting
        await parse
    assert time.monotonic() - started < 5
    await parser.close()


@pytest.mark.parametrize("delay", [0.0, 0.01, 0.03, 0.1])
def test_ctrl_c_while_workers_start_loses_no_parse(delay: float) -> None:
    # The terminal sends Ctrl-C's SIGINT to every process in the group, workers included,
    # and a worker still starting can't ignore it yet unless it started with it blocked
    # (DESIGN.md A.9). The script leads a process group of its own, as a crawl run from a
    # terminal does.
    done = subprocess.run(
        [sys.executable, "-m", "tests.support.pool_sigint", str(delay)],
        cwd=Path(__file__).parents[2],
        start_new_session=True,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"parsed": 8, "interrupted": True}
    assert "KeyboardInterrupt" not in done.stderr
    assert "died" not in done.stderr  # no worker was lost and replaced


def test_workers_exit_when_the_coordinator_is_killed() -> None:
    # They wait on a queue they hold both ends of, so without the coordinator they would
    # wait for ever.
    with subprocess.Popen(
        [sys.executable, "-m", "tests.support.pool_orphans"],
        cwd=Path(__file__).parents[2],
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        workers = json.loads(process.stdout.readline())
        assert len(set(workers)) == 2
        process.kill()
    deadline = time.monotonic() + 10
    while (alive := [pid for pid in workers if running(pid)]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert alive == []


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
