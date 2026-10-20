import importlib.metadata
import subprocess
import sys
from typing import NoReturn

import pytest

import web_crawler
from web_crawler.cli import main, parse_args
from web_crawler.config import Config
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
        "usage: crawl [-h] [--all-urls] [--max-pages N] [--max-concurrency N] [--max-rps R] "
        "[--parse-workers N] [--ignore-robots]"
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "the following arguments are required: URL"),
        (["https://example.com/", "--max-dept", "2"], "unrecognized arguments: --max-dept 2"),
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


def test_ctrl_c_before_the_crawl_handles_it_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(*args: object, **kwargs: object) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr("web_crawler.cli.crawl", interrupted)
    assert main(["https://example.com/"]) == ExitCode.INTERRUPTED
