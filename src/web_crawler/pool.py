"""The parse gateway: where parse_page runs (DESIGN.md §4.3, §5.4).

A pool of processes runs it, so that CPU-heavy parsing leaves the event loop; with no
workers, it runs inline. The pool starts a worker only when none is idle, and it guards
against the two sharp edges of ProcessPoolExecutor that DESIGN.md A.9 measured: a Ctrl-C
that reaches a worker while it starts, and a worker crash that fails every job in the pool.
"""

import asyncio
import logging
import math
import multiprocessing
import os
import signal
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing.context import BaseContext
from pathlib import Path
from typing import Protocol

from web_crawler.parse import parse_page
from web_crawler.types import ParseJob, ParseResult, WorkerContext

log = logging.getLogger(__name__)

# Ctrl-C reaches every process in the terminal's process group, workers included, but it's
# the coordinator's alone to handle (DESIGN.md §5.9).
_CTRL_C = {signal.SIGINT}
# A job that breaks this many pools is a suspect, and is parsed in a pool of its own.
_SUSPECT = 2


class ParseCrashed(Exception):
    """A page whose parse killed the worker running it, even with nothing else in its pool:
    a final error for that page alone."""


class Parser(Protocol):
    @property
    def workers(self) -> int:
        """The most parser processes it runs: 0 for parsing on the event loop."""
        ...

    async def parse(self, job: ParseJob) -> ParseResult:
        """job's result; raises ParseCrashed if parsing it kills a worker every time."""
        ...

    async def close(self) -> None:
        """Wait for the parses already submitted, then stop."""
        ...

    def kill(self) -> None:
        """Stop at once, abandoning parses in progress: for an exit that can't wait."""
        ...


class InlineParser:
    """parse_page on the event loop, for --parse-workers 0."""

    workers = 0

    def __init__(self, ctx: WorkerContext) -> None:
        self._ctx = ctx

    async def parse(self, job: ParseJob) -> ParseResult:
        return parse_page(job, self._ctx)

    async def close(self) -> None:
        pass

    def kill(self) -> None:
        pass


# What a worker parses with, set once by its initializer: the context crosses to each
# worker once, rather than with every job.
_ctx: WorkerContext | None = None


def init_worker(ctx: WorkerContext) -> None:
    """A worker's initializer (DESIGN.md §4.3).

    The worker ignores Ctrl-C. It started with SIGINT blocked, as ProcessParser submits under
    that mask, so even one that arrived while it started is ignored rather than fatal. It
    keeps SIGTERM's default: ProcessPoolExecutor stops the other workers of a broken pool
    with SIGTERM, and waits for them.

    It also exits when the coordinator does. Otherwise, waiting on a queue it holds both ends
    of, it would wait for ever after a kill -9 of the coordinator.
    """
    global _ctx  # noqa: PLW0603  (the worker's one piece of state)
    _ctx = ctx
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "pthread_sigmask"):  # pragma: no branch  (everywhere but Windows)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _CTRL_C)
    parent = multiprocessing.parent_process()
    assert parent is not None, "not a pool's worker"
    threading.Thread(target=_exit_after, args=(parent,), name="parent", daemon=True).start()


def _exit_after(parent: multiprocessing.process.BaseProcess) -> None:
    parent.join()
    os._exit(1)  # pragma: no cover  (tested, but the process ends before coverage saves)


def parse_in_worker(job: ParseJob) -> ParseResult:
    """What a worker runs for each job."""
    assert _ctx is not None, "init_worker hasn't run"
    return parse_page(job, _ctx)


class ProcessParser:
    """parse_page on up to `workers` processes, with crash isolation (DESIGN.md §5.4).

    A worker that dies, from a crash or a kill -9, breaks its whole pool: every job in it
    fails with BrokenProcessPool, not just the one that killed it. So the first job to see
    a pool broken replaces it, and each failed job is submitted again, without using up
    any retries. A job that breaks a pool a second time is a suspect: suspects are parsed
    one at a time in a pool of one worker of their own, while other pages carry on in the
    main pool. The one that breaks even that pool fails alone, with ParseCrashed.

    target is the module-level function the workers run; tests replace it.
    """

    def __init__(
        self,
        ctx: WorkerContext,
        workers: int,
        *,
        target: Callable[[ParseJob], ParseResult] = parse_in_worker,
        context: BaseContext | None = None,
    ) -> None:
        self._ctx = ctx
        self._target = target
        self._context = context or start_method()
        self._pool = self._new_pool(workers)
        self.workers = workers
        self._generation = 0  # how many times the main pool has been replaced
        self._isolation: ProcessPoolExecutor | None = None
        self._suspects = asyncio.Lock()  # one at a time in the isolation pool
        self._killed = False

    def _new_pool(self, workers: int) -> ProcessPoolExecutor:
        # A worker starts only when a job finds none idle (DESIGN.md A.9).
        return ProcessPoolExecutor(
            workers, self._context, initializer=init_worker, initargs=(self._ctx,)
        )

    async def parse(self, job: ParseJob) -> ParseResult:
        breaks = 0
        while True:
            generation = self._generation
            try:
                return await self._run(self._pool, job)
            except BrokenProcessPool:
                if self._killed:
                    raise
                breaks += 1
                if generation == self._generation:  # the first to see it broken
                    log.warning("a parser process died; starting its pool again")
                    # The broken pool stops its other workers and cleans up on its own.
                    self._pool = self._new_pool(self.workers)
                    self._generation += 1
                if breaks >= _SUSPECT:
                    return await self._parse_alone(job)

    async def _parse_alone(self, job: ParseJob) -> ParseResult:
        async with self._suspects:
            if self._isolation is None:
                self._isolation = self._new_pool(1)
            try:
                return await self._run(self._isolation, job)
            except BrokenProcessPool:
                self._isolation = None
                raise ParseCrashed(job.url) from None

    def _run(self, pool: ProcessPoolExecutor, job: ParseJob) -> asyncio.Future[ParseResult]:
        return asyncio.wrap_future(self._submit(pool, job))

    def _submit(self, pool: ProcessPoolExecutor, job: ParseJob) -> Future[ParseResult]:
        """Submit job with SIGINT blocked. submit() may start a worker, which inherits the
        mask, and so can't be killed by a Ctrl-C before init_worker has made it ignore one
        (DESIGN.md A.9). A Ctrl-C that arrives meanwhile waits until the mask is restored,
        and the coordinator handles it then."""
        if not hasattr(signal, "pthread_sigmask"):  # pragma: no cover  (Windows)
            return pool.submit(self._target, job)
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, _CTRL_C)
        try:
            return pool.submit(self._target, job)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)

    async def close(self) -> None:
        # On a thread of its own. shutdown() holds a lock for a moment that kill() needs, and
        # a second Ctrl-C runs kill() on the event loop's thread: were shutdown() running on
        # that thread too, the signal could arrive while it held the lock, and kill() would
        # wait for it for ever.
        await asyncio.to_thread(self._shutdown)

    def _shutdown(self) -> None:
        for pool in (self._pool, self._isolation):
            if pool is not None:
                pool.shutdown(wait=True)

    def kill(self) -> None:
        self._killed = True
        for pool in (self._pool, self._isolation):
            if pool is not None:
                pool.kill_workers()


def start(ctx: WorkerContext, workers: int) -> Parser:
    """The parser for a crawl: inline with no workers, otherwise a pool of at most workers
    processes, none of which starts until a page needs it."""
    return ProcessParser(ctx, workers) if workers else InlineParser(ctx)


def start_method() -> BaseContext:
    """How workers start (DESIGN.md §4.3). On Linux, forked from a server process that has
    already imported the main module and the parser, so each worker starts in milliseconds
    with them loaded; without __main__ in that list, every worker would import it again, and
    with it all of the crawler. Elsewhere, spawned, as Python does by default on macOS and
    Windows: each worker starts a new interpreter, and imports the main module itself."""
    if sys.platform == "linux":
        context = multiprocessing.get_context("forkserver")
        context.set_forkserver_preload(["__main__", "web_crawler.pool"])
        return context
    else:  # mypy skips the other platform's branch, but would flag code after the if
        return multiprocessing.get_context("spawn")


def worker_count(setting: int | None, *, root: Path = Path("/")) -> int:
    """How many parser processes a crawl may start: setting, from --parse-workers, if it's
    given; otherwise one fewer than the CPUs this process may use, leaving one for the
    coordinator."""
    if setting is not None:
        return setting
    return max(0, usable_cpus(root=root) - 1)


def usable_cpus(*, root: Path = Path("/")) -> int:
    """os.process_cpu_count(), which sees CPU affinity, capped by the CPU quota of the
    process's cgroup, which it doesn't see: in a container limited to 2 CPUs on a 64-core
    host, it says 64. PYTHON_CPU_COUNT and -X cpu_count set the count outright, and then
    the quota isn't applied either."""
    count = os.process_cpu_count() or 1
    if _cpu_count_set():
        return count
    quota = cgroup_cpu_quota(root=root)
    return count if quota is None else min(count, max(1, math.ceil(quota)))


def _cpu_count_set() -> bool:
    """Whether -X cpu_count or PYTHON_CPU_COUNT overrides the CPU count, as CPython reads
    them: the option wins over the variable, isolated mode ignores the variable, and
    "default" overrides nothing."""
    value = sys._xoptions.get("cpu_count")
    if value is None and not sys.flags.ignore_environment:
        value = os.environ.get("PYTHON_CPU_COUNT")
    return value not in (None, "default")


def cgroup_cpu_quota(*, root: Path = Path("/")) -> float | None:
    """The CPU quota of the process's cgroup, in CPUs, or None without one.

    A quota at any level above the process's cgroup applies to it too, so this is the
    lowest along the path to the root: cgroup v2's cpu.max, or v1's cpu.cfs_quota_us over
    cpu.cfs_period_us. root is "/" except in tests."""
    try:
        lines = (root / "proc/self/cgroup").read_text().splitlines()
    except OSError:  # not Linux
        return None
    quotas: list[float] = []
    for line in lines:
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" and not controllers:  # v2's single hierarchy
            quotas += _quotas(root / "sys/fs/cgroup", path, _v2_quota)
        elif "cpu" in controllers.split(","):
            for mount in ("cpu", "cpu,cpuacct", "cpuacct,cpu"):
                if (directory := root / "sys/fs/cgroup" / mount).is_dir():
                    quotas += _quotas(directory, path, _v1_quota)
                    break
    return min(quotas, default=None)


def _quotas(mount: Path, path: str, read: Callable[[Path], float | None]) -> list[float]:
    """The quotas set on path and each cgroup above it. In a container, path names the
    container's cgroup as the host sees it, and the mount is the container's own: the
    directories that don't exist are skipped, down to the mount's root."""
    parts = [part for part in path.split("/") if part]
    quotas = []
    for depth in range(len(parts), -1, -1):
        try:
            quota = read(mount.joinpath(*parts[:depth]))
        except OSError, ValueError:
            continue
        if quota is not None:
            quotas.append(quota)
    return quotas


def _v2_quota(directory: Path) -> float | None:
    quota, period = (directory / "cpu.max").read_text().split()
    return None if quota == "max" else int(quota) / int(period)


def _v1_quota(directory: Path) -> float | None:
    quota = int((directory / "cpu.cfs_quota_us").read_text())
    if quota <= 0:  # -1: no quota
        return None
    return quota / int((directory / "cpu.cfs_period_us").read_text())
