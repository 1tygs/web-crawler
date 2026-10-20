"""The command line: ``crawl URL [options]``."""

import argparse
import asyncio
import logging
import re
import sys
from collections.abc import Callable, Sequence

from web_crawler import __version__
from web_crawler.config import MIB, USER_AGENT, Config, ConfigError
from web_crawler.engine import crawl
from web_crawler.output import ThreadedSink, to_devnull
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
        "--max-concurrency",
        type=int,
        metavar="N",
        default=64,
        help="the most requests in flight at once; the crawl adapts below it to what the "
        "server sustains (default: %(default)s)",
    )
    parser.add_argument(
        "--max-rps",
        type=float,
        metavar="R",
        help="the most requests started per second; robots.txt's Crawl-delay can lower it",
    )
    parser.add_argument(
        "--parse-workers",
        type=_parse_workers,
        metavar="N",
        help="parser processes: auto for one fewer than the CPUs, within a container's "
        "quota, or a number; 0 parses in the crawler's own process (default: auto)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        metavar="S",
        default=60.0,
        help="seconds a request may take, from connecting to the end of its body "
        "(default: %(default)g)",
    )
    parser.add_argument(
        "--max-page-bytes",
        type=_size,
        metavar="N",
        default=32 * MIB,
        help="the most of a page that's read, and that it may decompress to: bytes, or with "
        "K, M or G for KiB, MiB or GiB; a page cut short is parsed as far as it got "
        "(default: 32M)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        metavar="N",
        default=3,
        help="how many times to try a page again after an error, such as a timeout, a "
        "dropped connection or a server error, waiting longer each time; a 429 or 503 is "
        "tried again up to 5 times whatever N is, as its Retry-After asks "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--no-skip-extensions",
        dest="skip_extensions",
        action="store_false",
        help="crawl links to files that are almost never HTML too, such as images, PDFs, "
        "archives, fonts, style sheets and scripts, as for a site whose /old.pdf redirects "
        "to a page; they're printed either way",
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


_SIZE = re.compile(r"([0-9]+)([KMG]?)", re.IGNORECASE)
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}  # binary, as for memory


def _size(value: str) -> int:
    """A number of bytes, with an optional binary suffix: 512K, 32M or 2G."""
    if (match := _SIZE.fullmatch(value)) is None:
        raise argparse.ArgumentTypeError(
            f"a number of bytes, with K, M or G if you like, not {value!r}"
        )
    return int(match[1]) * _UNITS[match[2].upper()]


def _parse_workers(value: str) -> int | None:
    if value == "auto":
        return None
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"auto or a number, not {value!r}") from None


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
            max_concurrency=args.max_concurrency,
            max_rps=args.max_rps,
            parse_workers=args.parse_workers,
            timeout=args.timeout,
            max_page_bytes=args.max_page_bytes,
            retries=args.retries,
            skip_extensions=args.skip_extensions,
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
        try:
            print(report.summary, file=sys.stderr, flush=True)
        except BrokenPipeError:
            # stderr went where stdout did, e.g. `2>&1 | head`, whose reader has gone.
            to_devnull(sys.stderr)
    return report.exit_code
