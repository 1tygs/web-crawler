import importlib.metadata
import subprocess
import sys

import pytest

import web_crawler
from web_crawler.cli import main, parse_args
from web_crawler.config import Config


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


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--help"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("usage: crawl [-h] [--version] URL")


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
