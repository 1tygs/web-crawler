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
    assert capsys.readouterr().out.startswith(
        "usage: crawl [-h] [--all-urls] [--ignore-robots] [--user-agent UA] [--version]"
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
