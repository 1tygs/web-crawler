"""Crawl settings (DESIGN.md §6.1): one frozen Config, validated when it's built.

The CLI and programmatic callers get the same checks, because they live here rather than in
the argument parser.
"""

import math
from dataclasses import dataclass

from web_crawler.urls import StartURLError, normalize_start

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
    timeout: float = 60.0  # seconds, for one whole request
    max_page_bytes: int = 32 * MIB  # body bytes read from one page
    max_pages: int | None = None  # stop once this many pages are printed
    all_urls: bool = False  # print every URL-bearing attribute; still crawl only links
    ignore_robots: bool = False  # for crawling your own site
    strict_host: bool = False  # a start URL that redirects to its www. variant can't start
    insecure: bool = False  # don't verify TLS certificates
    robots_refresh: float = 24 * 3600.0  # seconds between fetches of robots.txt (RFC 9309)

    def __post_init__(self) -> None:
        try:
            normalize_start(self.start_url)
        except StartURLError as error:
            raise ConfigError(str(error)) from None
        if self.max_concurrency < 1:
            raise ConfigError(f"max_concurrency must be at least 1, not {self.max_concurrency}")
        if self.parse_workers is not None and self.parse_workers < 0:
            raise ConfigError(f"parse_workers must be at least 0, not {self.parse_workers}")
        if self.max_rps is not None and not (math.isfinite(self.max_rps) and self.max_rps > 0):
            raise ConfigError(
                f"max_rps must be a positive number of requests per second, not {self.max_rps}"
            )
        if not (math.isfinite(self.timeout) and self.timeout > 0):
            raise ConfigError(f"timeout must be a positive number of seconds, not {self.timeout}")
        if not (math.isfinite(self.robots_refresh) and self.robots_refresh > 0):
            raise ConfigError(
                f"robots_refresh must be a positive number of seconds, not {self.robots_refresh}"
            )
        if self.max_page_bytes < 1:
            raise ConfigError(f"max_page_bytes must be at least 1, not {self.max_page_bytes}")
        if self.max_pages is not None and self.max_pages < 1:
            raise ConfigError(f"max_pages must be at least 1, not {self.max_pages}")
        if not (self.user_agent.strip() and self.user_agent.isascii()):
            raise ConfigError(f"user_agent must be non-blank ASCII, not {self.user_agent!r}")
        if not self.user_agent.isprintable():
            raise ConfigError(f"user_agent can't contain control characters: {self.user_agent!r}")
