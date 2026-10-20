"""How many parser processes a crawl gets, and how they start (DESIGN.md §4.3). The pool
itself runs real processes, so its tests are in tests/integration/test_pool.py."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from web_crawler import pool
from web_crawler.pool import InlineParser, ProcessParser, cgroup_cpu_quota, start, worker_count
from web_crawler.types import ParseJob, WorkerContext


def cgroups(root: Path, membership: str, files: dict[str, str]) -> Path:
    """A fake /proc/self/cgroup and /sys/fs/cgroup under root."""
    (root / "proc/self").mkdir(parents=True)
    (root / "proc/self/cgroup").write_text(membership)
    for name, content in files.items():
        path = root / "sys/fs/cgroup" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


@pytest.mark.parametrize(
    ("membership", "files", "quota"),
    [
        # cgroup v2, in a container with its own namespace: the container's cgroup is "/".
        ("0::/\n", {"cpu.max": "200000 100000\n"}, 2.0),
        ("0::/\n", {"cpu.max": "max 100000\n"}, None),
        # On a host: the lowest quota on the way up from the process's cgroup.
        (
            "0::/kubepods/pod/ctr\n",
            {
                "kubepods/pod/ctr/cpu.max": "max 100000\n",
                "kubepods/pod/cpu.max": "250000 100000\n",
                "kubepods/cpu.max": "800000 100000\n",
            },
            2.5,
        ),
        # A path the mount doesn't have, as in a container without its own namespace.
        ("0::/docker/abc\n", {"cpu.max": "150000 100000\n"}, 1.5),
        # cgroup v1, with cpu and cpuacct mounted together or apart.
        (
            "5:memory:/x\n4:cpu,cpuacct:/x\n",
            {
                "cpu,cpuacct/x/cpu.cfs_quota_us": "50000\n",
                "cpu,cpuacct/x/cpu.cfs_period_us": "100000\n",
            },
            0.5,
        ),
        (
            "4:cpu:/\n",
            {"cpu/cpu.cfs_quota_us": "-1\n", "cpu/cpu.cfs_period_us": "100000\n"},
            None,
        ),
        # Nothing to read, or nothing that makes sense.
        ("0::/\n", {}, None),
        ("0::/\n", {"cpu.max": "lots\n"}, None),
        ("4:cpu:/x\n", {}, None),
    ],
)
def test_cgroup_cpu_quota(
    tmp_path: Path, membership: str, files: dict[str, str], quota: float | None
) -> None:
    assert cgroup_cpu_quota(root=cgroups(tmp_path, membership, files)) == quota


def test_no_cgroups_no_quota(tmp_path: Path) -> None:
    assert cgroup_cpu_quota(root=tmp_path) is None  # macOS: there's no /proc


@pytest.fixture
def sixty_four_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "process_cpu_count", lambda: 64)
    monkeypatch.delenv("PYTHON_CPU_COUNT", raising=False)
    monkeypatch.setattr(sys, "_xoptions", {})


@pytest.mark.usefixtures("sixty_four_cpus")
def test_worker_count_keeps_a_cpu_for_the_coordinator(tmp_path: Path) -> None:
    assert worker_count(None, root=tmp_path) == 63
    assert worker_count(5, root=tmp_path) == 5  # --parse-workers
    assert worker_count(0, root=tmp_path) == 0  # inline


@pytest.mark.usefixtures("sixty_four_cpus")
@pytest.mark.parametrize(("quota", "workers"), [("250000", 2), ("100000", 0), ("10000", 0)])
def test_worker_count_stays_within_a_containers_quota(
    tmp_path: Path, quota: str, workers: int
) -> None:
    # 2.5 CPUs make 3, as a part-used CPU still runs a process; 0.1 make 1.
    root = cgroups(tmp_path, "0::/\n", {"cpu.max": f"{quota} 100000\n"})
    assert worker_count(None, root=root) == workers


@pytest.mark.usefixtures("sixty_four_cpus")
@pytest.mark.parametrize(
    ("xoptions", "variable", "workers"),
    [
        ({"cpu_count": "4"}, None, 63),  # set outright: the quota isn't applied
        ({}, "4", 63),
        ({"cpu_count": "default"}, "4", 1),  # the option overrides the variable
        ({}, "default", 1),
    ],
)
def test_an_explicit_cpu_count_overrides_the_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    xoptions: dict[str, str],
    variable: str | None,
    workers: int,
) -> None:
    # os.process_cpu_count() already gives the explicit count; it's 64 here.
    monkeypatch.setattr(sys, "_xoptions", xoptions)
    if variable is not None:
        monkeypatch.setenv("PYTHON_CPU_COUNT", variable)
    root = cgroups(tmp_path, "0::/\n", {"cpu.max": "200000 100000\n"})
    assert worker_count(None, root=root) == workers


def test_python_reads_an_explicit_cpu_count_as_pool_does() -> None:
    # The real interpreter, not monkeypatched flags: -X cpu_count and PYTHON_CPU_COUNT set
    # os.process_cpu_count(), and isolated mode ignores the variable.
    script = (
        "import os; from web_crawler import pool; "
        "print(os.process_cpu_count(), pool._cpu_count_set())"
    )
    for args, env, expected in [
        (["-X", "cpu_count=3"], {}, "3 True"),
        ([], {"PYTHON_CPU_COUNT": "3"}, "3 True"),
        (["-I"], {"PYTHON_CPU_COUNT": "3"}, None),
    ]:
        done = subprocess.run(
            [sys.executable, *args, "-c", script],
            env={"PATH": "/usr/bin:/bin", **env},
            capture_output=True,
            text=True,
            check=True,
        )
        if expected is None:
            assert done.stdout.split()[1] == "False"
        else:
            assert done.stdout.strip() == expected


@pytest.mark.parametrize(("platform", "method"), [("linux", "forkserver"), ("darwin", "spawn")])
def test_start_method(monkeypatch: pytest.MonkeyPatch, platform: str, method: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    assert pool.start_method().get_start_method() == method


def test_start_makes_an_inline_parser_without_workers() -> None:
    ctx = WorkerContext(("https://example.com/",), max_bytes=1 << 20)
    assert isinstance(start(ctx, 0), InlineParser)
    parser = start(ctx, 2)
    assert isinstance(parser, ProcessParser)
    parser._pool.shutdown()  # no worker started


async def test_the_inline_parser_parses_on_the_event_loop() -> None:
    parser = InlineParser(WorkerContext(("https://example.com/",), max_bytes=1 << 20))
    result = await parser.parse(ParseJob("https://example.com/", b'<a href="/a">a</a>'))
    assert result.links == [("https://example.com/a", "https://example.com/a")]
    await parser.close()
    parser.kill()
