import dataclasses
import math

import pytest

from web_crawler.config import MIB, USER_AGENT, Config, ConfigError


def test_defaults_follow_the_design() -> None:
    cfg = Config(start_url="https://example.com/")
    assert cfg.user_agent == USER_AGENT
    assert cfg.user_agent.startswith("web-crawler/")
    assert cfg.max_concurrency == 64
    assert cfg.parse_workers is None  # one fewer than the CPUs
    assert cfg.max_rps is None
    assert cfg.timeout == 60.0
    assert cfg.retries == 3
    assert cfg.skip_extensions
    assert (cfg.max_url_length, cfg.max_segment_repeats, cfg.max_query_variants) == (
        2048,
        3,
        1000,
    )
    assert cfg.max_page_bytes == 32 * MIB
    assert not cfg.all_urls
    assert cfg.max_pages is None
    assert cfg.max_depth is None
    assert not cfg.ignore_robots
    assert cfg.robots_refresh == 24 * 3600  # RFC 9309 §2.4
    assert (cfg.first_probe, cfg.give_up_after) == (1.0, 300.0)


def test_is_immutable() -> None:
    cfg = Config(start_url="https://example.com/")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.timeout = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_concurrency", 0, "max_concurrency must be at least 1"),
        ("max_concurrency", -3, "max_concurrency must be at least 1"),
        ("parse_workers", -1, "parse_workers must be at least 0"),
        ("max_rps", 0.0, "max_rps must be a positive number"),
        ("max_rps", -1.0, "max_rps must be a positive number"),
        ("max_rps", math.nan, "max_rps must be a positive number"),
        ("timeout", 0.0, "timeout must be a positive number"),
        ("timeout", -1.0, "timeout must be a positive number"),
        ("timeout", math.inf, "timeout must be a positive number"),
        ("timeout", math.nan, "timeout must be a positive number"),
        ("robots_refresh", 0.0, "robots_refresh must be a positive number"),
        ("robots_refresh", math.inf, "robots_refresh must be a positive number"),
        ("first_probe", 0.0, "first_probe must be a positive number of seconds, not 0.0"),
        ("give_up_after", -1.0, "give_up_after must be a positive number of seconds"),
        ("give_up_after", math.nan, "give_up_after must be a positive number of seconds"),
        ("max_page_bytes", 0, "max_page_bytes must be at least 1"),
        ("retries", -1, "retries must be at least 0"),
        ("max_url_length", 0, "max_url_length must be at least 1, not 0"),
        ("max_segment_repeats", 0, "max_segment_repeats must be at least 1"),
        ("max_query_variants", -5, "max_query_variants must be at least 1"),
        ("max_pages", 0, "max_pages must be at least 1"),
        ("max_depth", -1, "max_depth must be at least 0, not -1"),
        ("user_agent", "", "user_agent must be non-blank ASCII"),
        ("user_agent", "   ", "user_agent must be non-blank ASCII"),
        ("user_agent", "crawler/1.0 (über)", "user_agent must be non-blank ASCII"),
        ("user_agent", "crawler/1.0\r\nX-Injected: 1", "can't contain control characters"),
    ],
)
def test_rejects_values_out_of_range(field: str, value: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Config(start_url="https://example.com/", **{field: value})  # type: ignore[arg-type]


def test_config_error_is_a_value_error() -> None:
    assert issubclass(ConfigError, ValueError)


@pytest.mark.parametrize(
    ("start_url", "message"),
    [
        ("ftp://example.com/", "isn't an http or https URL"),
        ("https://user:pw@example.com/", "contains credentials"),
        ("https://exa mple.com/", "isn't a valid URL"),
    ],
)
def test_rejects_a_start_url_that_cant_be_crawled(start_url: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Config(start_url=start_url)
