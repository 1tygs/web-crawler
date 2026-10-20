import importlib.metadata
import subprocess
import sys

import pytest

import web_crawler
from web_crawler.cli import main


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
