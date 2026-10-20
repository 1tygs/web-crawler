"""Crawl settings (DESIGN.md §6.1): one frozen Config, validated when it's built.

The CLI and programmatic callers get the same checks, because they live here rather than in
the argument parser.
"""

import math
from dataclasses import dataclass

from web_crawler.records import FORMATS
from web_crawler.types import Format
from web_crawler.urls import (
    MAX_QUERY_VARIANTS,
    MAX_SEGMENT_REPEATS,
    MAX_URL_LENGTH,
    StartURLError,
    normalize_start,
)

MIB = 1024 * 1024

# The product token, web-crawler, is also the name a robots.txt group uses to address us.
USER_AGENT = "web-crawler/1.0 (+https://github.com/1tygs/web-crawler)"


class ConfigError(ValueError):
    """A setting is invalid or out of range. The CLI reports it as a usage error."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    start_url: str  # as given; urls.normalize_start makes it a display URL
    user_agent: str = USER_AGENT
    max_concurrency: int = 64  # upper bound on requests in flight
    parse_workers: int | None = None  # parser processes; None: one fewer than the CPUs
    max_rps: float | None = None  # requests started per second, at most
    timeout: float = 60.0  # seconds for one whole request, from connecting to the body's end
    retries: int = 3  # how many times an item is tried again after an error; not a 429 or 503
    # The most of a page's body that's read, as sent, and that it decompresses to.
    max_page_bytes: int = 32 * MIB
    max_pages: int | None = None  # stop once this many pages are printed
    max_depth: int | None = None  # crawl only pages within this many links of the start URL
    format: Format = "text"  # the records' (DESIGN.md §5.8)
    include_errors: bool = False  # a JSONL record for each item that ends without a page too
    all_urls: bool = False  # print every URL-bearing attribute; still crawl only links
    ignore_robots: bool = False  # for crawling your own site
    strict_host: bool = False  # a start URL that redirects to its www. variant can't start
    insecure: bool = False  # don't verify TLS certificates
    skip_extensions: bool = True  # don't crawl links to files that are almost never HTML
    # The trap rules' thresholds (DESIGN.md §5.1), which have no flags.
    max_url_length: int = MAX_URL_LENGTH  # characters
    max_segment_repeats: int = MAX_SEGMENT_REPEATS  # times one segment may occur in a path
    max_query_variants: int = MAX_QUERY_VARIANTS  # distinct query strings for one path
    robots_refresh: float = 24 * 3600.0  # seconds between fetches of robots.txt (RFC 9309)
    # A host the crawl pauses for, as unreachable, is checked first_probe seconds after the
    # pause began, then twice as long after each check, up to a minute apart; after
    # give_up_after seconds the crawl aborts.
    first_probe: float = 1.0
    give_up_after: float = 300.0

    def __post_init__(self) -> None:
        try:
            normalize_start(self.start_url)
        except StartURLError as error:
            raise ConfigError(str(error)) from None
        for name, least in _AT_LEAST.items():
            if (count := getattr(self, name)) is not None and count < least:
                raise ConfigError(f"{name} must be at least {least}, not {count}")
        for name, unit in _POSITIVE.items():
            value = getattr(self, name)
            if value is not None and not (math.isfinite(value) and value > 0):
                raise ConfigError(f"{name} must be a positive number of {unit}, not {value}")
        if not (self.user_agent.strip() and self.user_agent.isascii()):
            raise ConfigError(f"user_agent must be non-blank ASCII, not {self.user_agent!r}")
        if not self.user_agent.isprintable():
            raise ConfigError(f"user_agent can't contain control characters: {self.user_agent!r}")
        if self.format not in FORMATS:
            raise ConfigError(f"format must be text or jsonl, not {self.format!r}")
        if self.include_errors and self.format != "jsonl":
            # A text record is a page's links: an error has none (plan §9 item 9).
            raise ConfigError("include_errors needs format jsonl: errors have JSONL records only")


# The least each count may be, and the unit of each number that must be positive. A setting
# that may be None, for no limit, is checked only when it's set.
_AT_LEAST = {
    "max_concurrency": 1,
    "parse_workers": 0,
    "retries": 0,
    "max_page_bytes": 1,
    "max_pages": 1,
    "max_depth": 0,
    "max_url_length": 1,
    "max_segment_repeats": 1,
    "max_query_variants": 1,
}
_POSITIVE = {
    "max_rps": "requests per second",
    "timeout": "seconds",
    "robots_refresh": "seconds",
    "first_probe": "seconds",
    "give_up_after": "seconds",
}
