"""The command line: ``crawl URL [options]``."""

import argparse
import sys
from collections.abc import Sequence

from web_crawler import __version__
from web_crawler.config import Config, ConfigError
from web_crawler.types import ExitCode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl",
        description="Crawl one host and print each page's URL followed by the links on it.",
        suggest_on_error=True,
    )
    parser.add_argument(
        "url", metavar="URL", help="where to start; https:// is assumed if there's no scheme"
    )
    parser.add_argument("--version", action="version", version=f"web-crawler {__version__}")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> Config:
    """Build the Config for argv; exit with status 2 on a usage error, as argparse does."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return Config(start_url=args.url)
    except ConfigError as error:
        parser.error(str(error))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    parse_args(argv)
    print("crawl: crawling isn't implemented yet", file=sys.stderr)
    return ExitCode.INTERNAL_ERROR
