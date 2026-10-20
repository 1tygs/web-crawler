"""Crawl settings (DESIGN.md §6.1): one frozen Config, validated when it's built.

The CLI and programmatic callers get the same checks, because they live here rather than in
the argument parser.
"""

import math
from dataclasses import dataclass

MIB = 1024 * 1024

# The product token, web-crawler, is also the name a robots.txt group uses to address us.
USER_AGENT = "web-crawler/1.0 (+https://github.com/1tygs/web-crawler)"


class ConfigError(ValueError):
    """A setting is out of range. The CLI reports it as a usage error."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    start_url: str  # as given; urls.normalize_start makes it a URL
    user_agent: str = USER_AGENT
    max_concurrency: int = 64  # upper bound on requests in flight
    timeout: float = 60.0  # seconds, for one whole request
    max_page_bytes: int = 32 * MIB  # body bytes read from one page

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ConfigError(f"max_concurrency must be at least 1, not {self.max_concurrency}")
        if not (math.isfinite(self.timeout) and self.timeout > 0):
            raise ConfigError(f"timeout must be a positive number of seconds, not {self.timeout}")
        if self.max_page_bytes < 1:
            raise ConfigError(f"max_page_bytes must be at least 1, not {self.max_page_bytes}")
        if not (self.user_agent.strip() and self.user_agent.isascii()):
            raise ConfigError(f"user_agent must be non-blank ASCII, not {self.user_agent!r}")
        if not self.user_agent.isprintable():
            raise ConfigError(f"user_agent can't contain control characters: {self.user_agent!r}")
