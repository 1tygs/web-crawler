"""The command line: ``crawl URL [options]``."""

import argparse
from collections.abc import Sequence

from web_crawler import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl",
        description="Crawl one host and print each page's URL followed by the links on it.",
    )
    parser.add_argument("--version", action="version", version=f"web-crawler {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    _parser().parse_args(argv)
    return 0
