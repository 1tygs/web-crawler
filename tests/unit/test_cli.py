import importlib.metadata
import subprocess
import sys
from typing import NoReturn

import pytest

import web_crawler
from web_crawler.cli import main, parse_args
from web_crawler.config import Config
from web_crawler.engine import CrawlReport
from web_crawler.stats import Stats
from web_crawler.types import ExitCode


def test_version_matches_the_distribution_metadata() -> None:
    assert web_crawler.__version__ == importlib.metadata.version("web-crawler")


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"web-crawler {web_crawler.__version__}\n"


def test_python_dash_m_runs_the_cli() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "web_crawler", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == f"web-crawler {web_crawler.__version__}\n"
    assert done.stderr == ""


def test_builds_a_config_from_the_url() -> None:
    assert parse_args(["https://example.com/"]) == Config(start_url="https://example.com/")


def test_all_urls() -> None:
    assert parse_args(["--all-urls", "https://example.com/"]).all_urls


def test_max_pages() -> None:
    assert parse_args(["https://example.com/", "--max-pages", "5"]).max_pages == 5
    assert parse_args(["https://example.com/"]).max_pages is None


def test_max_pages_below_1_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["https://example.com/", "--max-pages", "0"])
    assert exit_info.value.code == 2
    assert "max_pages must be at least 1, not 0" in capsys.readouterr().err


def test_max_depth() -> None:
    assert parse_args(["https://example.com/", "--max-depth", "2"]).max_depth == 2
    assert parse_args(["https://example.com/", "--max-depth", "0"]).max_depth == 0
    assert parse_args(["https://example.com/"]).max_depth is None


def test_max_depth_below_0_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["https://example.com/", "--max-depth", "-1"])
    assert exit_info.value.code == 2
    assert "max_depth must be at least 0, not -1" in capsys.readouterr().err


def test_speed_options() -> None:
    speed = ["--max-concurrency", "10", "--max-rps", "2.5", "--parse-workers", "3"]
    cfg = parse_args(["https://example.com/", *speed])
    assert (cfg.max_concurrency, cfg.max_rps, cfg.parse_workers) == (10, 2.5, 3)
    cfg = parse_args(["https://example.com/", "--parse-workers", "auto"])
    assert (cfg.max_concurrency, cfg.max_rps, cfg.parse_workers) == (64, None, None)
    assert parse_args(["https://example.com/", "--parse-workers", "0"]).parse_workers == 0


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--parse-workers", "some"], "argument --parse-workers: auto or a number, not 'some'"),
        (["--parse-workers", "-1"], "parse_workers must be at least 0, not -1"),
        (["--max-rps", "0"], "max_rps must be a positive number of requests per second, not 0.0"),
        (["--max-rps", "inf"], "max_rps must be a positive number"),
        (["--max-concurrency", "0"], "max_concurrency must be at least 1, not 0"),
    ],
)
def test_speed_options_out_of_range_are_usage_errors(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["https://example.com/", *argv])
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_timeout_and_max_page_bytes() -> None:
    cfg = parse_args(["https://example.com/", "--timeout", "2.5", "--max-page-bytes", "4096"])
    assert (cfg.timeout, cfg.max_page_bytes) == (2.5, 4096)
    cfg = parse_args(["https://example.com/"])
    assert (cfg.timeout, cfg.max_page_bytes) == (60.0, 32 * 1024 * 1024)


@pytest.mark.parametrize(
    ("value", "size"),
    [("1", 1), ("512K", 512 * 1024), ("512k", 512 * 1024), ("32M", 32 << 20), ("2G", 2 << 30)],
)
def test_max_page_bytes_takes_binary_suffixes(value: str, size: int) -> None:
    assert parse_args(["https://example.com/", "--max-page-bytes", value]).max_page_bytes == size


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--max-page-bytes", "32MB"], "a number of bytes, with K, M or G if you like, not '32MB'"),
        (["--max-page-bytes", "-1"], "a number of bytes"),
        (["--max-page-bytes", "1.5M"], "a number of bytes"),
        (["--max-page-bytes", "0"], "max_page_bytes must be at least 1, not 0"),
        (["--timeout", "0"], "timeout must be a positive number of seconds, not 0.0"),
        (["--timeout", "inf"], "timeout must be a positive number of seconds"),
        (["--timeout", "soon"], "argument --timeout: invalid float value: 'soon'"),
    ],
)
def test_timeout_and_max_page_bytes_out_of_range_are_usage_errors(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["https://example.com/", *argv])
    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


def test_retries() -> None:
    assert parse_args(["https://example.com/", "--retries", "0"]).retries == 0
    assert parse_args(["https://example.com/", "--retries", "7"]).retries == 7
    assert parse_args(["https://example.com/"]).retries == 3


def test_negative_retries_are_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["https://example.com/", "--retries", "-1"])
    assert exit_info.value.code == 2
    assert "retries must be at least 0, not -1" in capsys.readouterr().err


def test_no_skip_extensions() -> None:
    assert not parse_args(["https://example.com/", "--no-skip-extensions"]).skip_extensions
    assert parse_args(["https://example.com/"]).skip_extensions


def test_start_url_options() -> None:
    cfg = parse_args(["https://example.com/", "--strict-host", "--insecure"])
    assert (cfg.strict_host, cfg.insecure) == (True, True)
    cfg = parse_args(["https://example.com/"])
    assert (cfg.strict_host, cfg.insecure) == (False, False)


def test_robots_options() -> None:
    cfg = parse_args(["https://example.com/", "--ignore-robots", "--user-agent", "MyBot/2.0"])
    assert cfg.ignore_robots
    assert cfg.user_agent == "MyBot/2.0"
    assert not parse_args(["https://example.com/"]).ignore_robots


def test_an_invalid_user_agent_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["https://example.com/", "--user-agent", " "])
    assert exit_info.value.code == 2
    assert "user_agent must be non-blank ASCII" in capsys.readouterr().err


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--help"])
    assert exit_info.value.code == 0
    usage = " ".join(capsys.readouterr().out.split())  # however the terminal's width wraps it
    assert usage.startswith(
        "usage: crawl [-h] [--all-urls] [--max-pages N] [--max-depth N] [--max-concurrency N] "
        "[--max-rps R] "
        "[--parse-workers N] [--timeout S] [--max-page-bytes N] [--retries N] "
        "[--no-skip-extensions] [--ignore-robots]"
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "the following arguments are required: URL"),
        (["https://example.com/", "--max-deph", "2"], "unrecognized arguments: --max-deph 2"),
        (["https://a.example/", "https://b.example/"], "unrecognized arguments"),
    ],
)
def test_usage_errors_exit_2(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(argv)
    assert exit_info.value.code == 2
    stderr = capsys.readouterr().err
    assert stderr.startswith("usage: crawl")
    assert message in stderr


def test_an_uncrawlable_url_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["ftp://example.com/"])
    assert exit_info.value.code == 2
    assert (
        "crawl: error: 'ftp://example.com/' isn't an http or https URL" in capsys.readouterr().err
    )


def test_an_unexpected_exception_exits_1_with_its_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("a bug")

    # main() builds the crawl itself, so the engine is replaced where main() looks it up.
    monkeypatch.setattr("web_crawler.cli.crawl", broken)
    assert main(["https://example.com/"]) == ExitCode.INTERNAL_ERROR
    assert "internal error" in caplog.text
    assert "RuntimeError: a bug" in caplog.text


@pytest.mark.parametrize("code", [ExitCode.ABORTED, ExitCode.STDOUT_CLOSED, ExitCode.TERMINATED])
def test_a_crawl_that_stops_early_prints_its_summary_and_exits_with_its_code(
    code: ExitCode, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def stopped(*args: object, **kwargs: object) -> CrawlReport:
        return CrawlReport(code, Stats(started=0.0, pages=3), unique_urls=7, finished=2.0)

    monkeypatch.setattr("web_crawler.cli.crawl", stopped)
    assert main(["https://example.com/"]) == code
    assert capsys.readouterr().err.startswith("3 pages in 2.0 s")


def test_ctrl_c_before_the_crawl_handles_it_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(*args: object, **kwargs: object) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr("web_crawler.cli.crawl", interrupted)
    assert main(["https://example.com/"]) == ExitCode.INTERRUPTED
