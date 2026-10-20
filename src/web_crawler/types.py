"""Contracts shared by the core and the shell (DESIGN.md §6.2).

The dataclasses are frozen and slotted: an item is created once per admitted URL and never
changed, and the parse contracts cross a process boundary by pickle.
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import NamedTuple


@dataclass(frozen=True, slots=True)
class CrawlItem:
    """A URL that admission accepted, waiting for or undergoing its fetch."""

    url: str  # display URL: what is requested and printed
    key: str  # dedupe key
    depth: int  # links from the start URL


@dataclass(frozen=True, slots=True)
class ParseJob:
    """A page for a parser: coordinator to worker."""

    url: str  # final URL after redirects, which is also the base URL
    body: bytes  # as it arrived: still compressed, as content_encoding says
    http_charset: str | None = None  # the Content-Type header's charset parameter
    truncated: bool = False  # the body was cut at the size cap
    content_encoding: str | None = None  # the Content-Encoding header


class ParseResult(NamedTuple):
    """What a parser returns: worker to coordinator."""

    record: bytes  # rendered output, written verbatim; b"" when there's an error
    links: list[tuple[str, str]]  # in-scope (key, url) pairs, unique by key, in document order
    links_found: int  # links printed on the page, for stats
    decoded_bytes: int  # the body's size once decompressed, for stats
    error: str | None  # why the page can't be printed, e.g. its body can't be decompressed


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """What every parse needs and never changes during a crawl, sent to each worker once."""

    scope_prefixes: tuple[str, ...]
    max_bytes: int  # the most a body may decompress to; the rest is cut off
    all_urls: bool = False  # print every URL-bearing attribute, not only navigational links


class ExitCode(IntEnum):
    """The process exit codes of DESIGN.md §6.1."""

    OK = 0
    INTERNAL_ERROR = 1
    USAGE = 2
    CANNOT_START = 3
    INTERRUPTED = 130  # SIGINT
    TERMINATED = 143  # SIGTERM
