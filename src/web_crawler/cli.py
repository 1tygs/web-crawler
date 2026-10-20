"""The command line: ``crawl URL [options]``."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence

from web_crawler import __version__
from web_crawler.config import USER_AGENT, Config, ConfigError
from web_crawler.engine import crawl
from web_crawler.output import ThreadedSink
from web_crawler.types import ExitCode

log = logging.getLogger(__name__)

# uvloop costs about 10% less CPU per request (DESIGN.md D1). It doesn't support Windows,
# which gets asyncio's default loop.
_loop_factory: Callable[[], asyncio.AbstractEventLoop] | None
if sys.platform == "win32":  # pragma: no cover
    _loop_factory = None
else:
    import uvloop

    _loop_factory = uvloop.new_event_loop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl",
        description="Crawl one host and print each page's URL followed by the links on it.",
        suggest_on_error=True,
    )
    parser.add_argument(
        "url", metavar="URL", help="where to start; https:// is assumed if there's no scheme"
    )
    parser.add_argument(
        "--all-urls",
        action="store_true",
        help="print every URL on each page, from images, scripts, forms and the like too, in "
        "every scheme; only links are crawled",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        metavar="N",
        help="stop once N pages are printed; Ctrl-C also stops cleanly, printing whole records",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="don't fetch or obey robots.txt: for crawling your own site",
    )
    parser.add_argument(
        "--strict-host",
        action="store_true",
        help="refuse a start URL that redirects to another host, even when only www. differs",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="don't verify TLS certificates: for development hosts",
    )
    parser.add_argument(
        "--user-agent",
        metavar="UA",
        default=USER_AGENT,
        help="the User-Agent header; its product token, the leading letters, hyphens and "
        "underscores, also selects the robots.txt group to obey (default: %(default)s)",
    )
    parser.add_argument("--version", action="version", version=f"web-crawler {__version__}")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> Config:
    """Build the Config for argv; exit with status 2 on a usage error, as argparse does."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return Config(
            start_url=args.url,
            user_agent=args.user_agent,
            all_urls=args.all_urls,
            max_pages=args.max_pages,
            ignore_robots=args.ignore_robots,
            strict_host=args.strict_host,
            insecure=args.insecure,
        )
    except ConfigError as error:
        parser.error(str(error))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    cfg = parse_args(argv)
    logging.basicConfig(format="crawl: %(message)s", level=logging.WARNING, stream=sys.stderr)
    try:
        report = asyncio.run(
            crawl(cfg, ThreadedSink(sys.stdout.buffer)), loop_factory=_loop_factory
        )
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED
    except Exception:
        log.exception("internal error")
        return ExitCode.INTERNAL_ERROR
    if report.exit_code is not ExitCode.CANNOT_START:
        print(report.summary, file=sys.stderr)
    return report.exit_code
