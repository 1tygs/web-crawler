"""The command line: ``crawl URL [options]``."""

import argparse
import asyncio
import contextlib
import logging
import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from typing import BinaryIO, TextIO

from web_crawler import __version__
from web_crawler.config import MIB, USER_AGENT, Config, ConfigError
from web_crawler.engine import crawl
from web_crawler.output import ThreadedSink, to_devnull
from web_crawler.records import FORMATS
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
        "--max-depth",
        type=int,
        metavar="N",
        help="crawl only pages within N links of the start URL, exactly, however the "
        "fetches interleave; 0 crawls the start URL alone",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default="text",
        help="text prints each page's URL, then each link on it indented by two spaces, then "
        "a blank line; jsonl prints a JSON object per page, with its url, status, depth and "
        "links, and redirected_from, truncated and canonical when there's something to say "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--include-errors",
        action="store_true",
        help="with --format jsonl, also print a record for each URL that ended without a "
        "page, saying why: an error, a response that isn't HTML, or a redirect to a page "
        "that another link leads to",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="write the records to FILE, replacing it, rather than to stdout",
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
    noise = parser.add_mutually_exclusive_group()
    noise.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="log the URLs that fail, the first few that each rule keeps from being crawled, "
        "and the concurrency limit's decisions; -vv logs every request and every URL not "
        "crawled too",
    )
    noise.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print errors only on stderr: no progress line and no summary",
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
    return _config(parser, parser.parse_args(argv))


def _config(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Config:
    try:
        return Config(
            start_url=args.url,
            user_agent=args.user_agent,
            all_urls=args.all_urls,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            format=args.format,
            include_errors=args.include_errors,
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
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = _config(parser, args)
    try:
        # Opened before the crawl starts, so that a path that can't be written to is a
        # usage error rather than a crawl's worth of output lost.
        output = open(args.output, "wb") if args.output is not None else None  # noqa: SIM115
    except OSError as error:
        parser.error(f"can't write to {args.output}: {error.strerror}")
    try:
        return _run(cfg, output, verbose=args.verbose, quiet=args.quiet)
    finally:
        if output is not None:
            output.close()


# The tip for a crawl whose records went to the terminal (DESIGN.md §5.8).
TIP = "tip: -o FILE, or > FILE, is faster than printing to the terminal, and shows progress"


def _run(cfg: Config, output: BinaryIO | None, *, verbose: int, quiet: bool) -> int:
    """Crawl, with records going to output, or to stdout, and logs, the progress line and
    the summary to stderr. The progress line shows only when stderr is a terminal and the
    records go elsewhere: drawn between records on the same terminal, it would garble them.
    Records that went to the terminal get a tip instead, after the summary."""
    stderr_shown = sys.stderr.isatty() and not quiet
    records_shown = output is None and sys.stdout.isatty()
    progress = _ProgressLine(sys.stderr) if stderr_shown and not records_shown else None
    level = logging.ERROR if quiet else max(logging.DEBUG, logging.WARNING - 10 * verbose)
    with _logging(progress or logging.StreamHandler(sys.stderr), level):
        try:
            sink = ThreadedSink(output or sys.stdout.buffer)
            report = asyncio.run(crawl(cfg, sink, progress=progress), loop_factory=_loop_factory)
        except KeyboardInterrupt:
            return ExitCode.INTERRUPTED
        except Exception:
            log.exception("internal error")
            return ExitCode.INTERNAL_ERROR
    if report.exit_code is not ExitCode.CANNOT_START and not quiet:
        lines = [report.summary] + ([TIP] if stderr_shown and records_shown else [])
        try:
            print("\n".join(lines), file=sys.stderr, flush=True)
        except BrokenPipeError:
            # stderr went where stdout did, e.g. `2>&1 | head`, whose reader has gone.
            to_devnull(sys.stderr)
    return report.exit_code


@contextlib.contextmanager
def _logging(handler: logging.Handler, level: int) -> Iterator[None]:
    """Log through handler: the crawler's records from level up, and other libraries' from
    WARNING up, or ERROR under -q. As it was before, after."""
    handler.setFormatter(logging.Formatter("crawl: %(message)s"))
    root, crawler = logging.getLogger(), logging.getLogger("web_crawler")
    before = root.level, crawler.level
    root.addHandler(handler)
    root.setLevel(max(level, logging.WARNING))
    crawler.setLevel(level)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(before[0])
        crawler.setLevel(before[1])


class _ProgressLine(logging.StreamHandler[TextIO]):
    """The progress line, on a terminal (DESIGN.md §10), with the log above it. A record
    logged takes the line away, is written, and the line is drawn again below it, so that
    neither tears the other. The line is cut to the terminal's width, so that a carriage
    return goes back to its start."""

    def __init__(self, stream: TextIO) -> None:
        super().__init__(stream)
        self._line = ""  # on the terminal now
        self._cleared = False  # for good: the crawl is over

    def show(self, line: str) -> None:
        self.acquire()
        try:
            if not self._cleared:
                self._line = line[: self._width() - 1]
                self.stream.write(f"\r{self._line}\x1b[K")
                self.stream.flush()
        finally:
            self.release()

    def clear(self) -> None:
        self.acquire()
        try:
            self._erase()
            self._cleared = True
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        # handle() holds the lock.
        line = self._line
        self._erase()
        super().emit(record)
        if line:
            self.show(line)

    def _erase(self) -> None:
        if self._line:
            self.stream.write("\r\x1b[K")
            self.stream.flush()
            self._line = ""

    def _width(self) -> int:
        """The terminal's width, or 80 columns if it doesn't say."""
        try:
            return os.get_terminal_size(self.stream.fileno()).columns or 80
        except OSError, ValueError:
            return 80
