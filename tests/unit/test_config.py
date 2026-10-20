import dataclasses
import math

import pytest

from web_crawler.config import MIB, USER_AGENT, Config, ConfigError


def test_defaults_follow_the_design() -> None:
    cfg = Config(start_url="https://example.com/")
    assert cfg.user_agent == USER_AGENT
    assert cfg.user_agent.startswith("web-crawler/")
    assert cfg.max_concurrency == 64
    assert cfg.timeout == 60.0
    assert cfg.max_page_bytes == 32 * MIB


def test_is_immutable() -> None:
    cfg = Config(start_url="https://example.com/")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.timeout = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_concurrency", 0, "max_concurrency must be at least 1"),
        ("max_concurrency", -3, "max_concurrency must be at least 1"),
        ("timeout", 0.0, "timeout must be a positive number"),
        ("timeout", -1.0, "timeout must be a positive number"),
        ("timeout", math.inf, "timeout must be a positive number"),
        ("timeout", math.nan, "timeout must be a positive number"),
        ("max_page_bytes", 0, "max_page_bytes must be at least 1"),
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
