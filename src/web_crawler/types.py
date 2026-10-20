"""Contracts shared by the core and the shell (DESIGN.md §6.2).

The dataclasses are frozen and slotted: an item is created once per admitted URL and never
changed, and the parse contracts cross a process boundary by pickle.
"""

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import NamedTuple


@dataclass(frozen=True, slots=True)
class CrawlItem:
    """A URL that admission accepted, waiting for or undergoing its fetch."""

    url: str  # display URL: what is requested and printed
    key: str  # dedupe key
    depth: int  # links from the start URL
    attempt: int = 0  # retries after errors so far (--retries)
    throttled: int = 0  # retries after a 429 or 503 so far, which have a budget of their own
    # The redirects already followed to url, in order. A retry resumes where its chain got
    # to: the URLs before it are this item's claims, which no other item will request.
    redirected_from: tuple[str, ...] = ()


class ErrorKind(StrEnum):
    """Why a request got no response, or its body couldn't be read: the transport's
    errors, which retry.classify() and the concurrency limiter read."""

    DNS = "dns"
    CONNECT = "connect"
    TLS = "tls"
    CONNECT_TIMEOUT = "connect_timeout"
    TIMEOUT = "timeout"
    RESET = "reset"  # the connection was closed or reset
    PAYLOAD = "payload"  # the body ended early
    PROTOCOL = "protocol"  # the response wasn't valid HTTP
    INVALID_URL = "invalid_url"


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
    truncated: bool = False  # the body was cut at the size cap, or its stream ended early


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
    ABORTED = 4  # the host stopped answering, or kept refusing requests
    INTERRUPTED = 130  # SIGINT
    TERMINATED = 143  # SIGTERM
