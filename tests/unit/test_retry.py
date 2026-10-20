"""DESIGN.md §5.4's table, as retry.classify() reads it, and the backoff between tries."""

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.support.jitter import Highest, Lowest
from web_crawler.retry import CAP, THROTTLED_RETRIES, Class, backoff, classify, next_attempt
from web_crawler.types import CrawlItem, ErrorKind

ITEM = CrawlItem("https://example.com/a", "https://example.com/a", depth=2)


@pytest.mark.parametrize(
    "kind",
    [
        ErrorKind.DNS,
        ErrorKind.CONNECT,  # refused
        ErrorKind.RESET,
        ErrorKind.TLS,
        ErrorKind.CONNECT_TIMEOUT,
        ErrorKind.TIMEOUT,
        ErrorKind.PAYLOAD,  # the body cut off mid-transfer
    ],
)
def test_no_response_or_a_body_cut_off_is_an_error(kind: ErrorKind) -> None:
    assert classify(kind) is Class.ERROR


@pytest.mark.parametrize("kind", [ErrorKind.PROTOCOL, ErrorKind.INVALID_URL])
def test_a_response_that_isnt_http_or_a_url_that_cant_be_requested_is_final(
    kind: ErrorKind,
) -> None:
    assert classify(kind) is Class.FINAL


@pytest.mark.parametrize("status", [429, 503])
def test_429_and_503_are_throttling(status: int) -> None:
    assert classify(status) is Class.THROTTLE


@pytest.mark.parametrize("status", [408, 500, 502, 504])
def test_timeouts_and_failures_along_the_way_are_errors(status: int) -> None:
    assert classify(status) is Class.ERROR


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 410, 418, 451, 501, 505, 599])
def test_other_error_statuses_are_final(status: int) -> None:
    assert classify(status) is Class.FINAL


@pytest.mark.parametrize("retries", range(8))
def test_backoff_is_full_jitter_doubling_from_half_a_second_up_to_30(retries: int) -> None:
    ceiling = min(CAP, 0.5 * 2**retries)
    assert backoff(retries, Lowest()) == 0.0
    assert backoff(retries, Highest()) == ceiling
    rng = random.Random(retries)
    assert all(0.0 <= backoff(retries, rng) <= ceiling for _ in range(100))


def test_backoff_stays_at_the_cap_however_many_retries() -> None:
    assert backoff(10_000, Highest()) == CAP


def test_an_error_uses_up_one_of_the_retries() -> None:
    again = next_attempt(ITEM, Class.ERROR, retries=3, rng=Highest())
    assert again == (CrawlItem(ITEM.url, ITEM.key, depth=2, attempt=1), 0.5)
    item, _ = again
    assert next_attempt(item, Class.ERROR, retries=3, rng=Highest()) == (
        CrawlItem(ITEM.url, ITEM.key, depth=2, attempt=2),
        1.0,
    )


def test_an_error_with_no_retries_left_is_final() -> None:
    spent = CrawlItem(ITEM.url, ITEM.key, depth=2, attempt=3)
    assert next_attempt(spent, Class.ERROR, retries=3, rng=Highest()) is None
    assert next_attempt(ITEM, Class.ERROR, retries=0, rng=Highest()) is None


def test_throttling_has_a_budget_of_its_own() -> None:
    # An item that has used up its retries for errors may still be throttled five times,
    # and throttling leaves its error retries alone.
    item = CrawlItem(ITEM.url, ITEM.key, depth=2, attempt=3)
    delays = []
    for throttled in range(THROTTLED_RETRIES):
        again = next_attempt(item, Class.THROTTLE, retries=3, rng=Highest())
        assert again is not None
        item, delay = again
        assert (item.attempt, item.throttled) == (3, throttled + 1)
        delays.append(delay)
    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert next_attempt(item, Class.THROTTLE, retries=3, rng=Highest()) is None


def test_throttling_waits_as_long_as_retry_after_says() -> None:
    again = next_attempt(ITEM, Class.THROTTLE, retries=3, rng=Highest(), retry_after=120.0)
    assert again == (CrawlItem(ITEM.url, ITEM.key, depth=2, throttled=1), 120.0)


def test_a_final_failure_is_never_tried_again() -> None:
    assert next_attempt(ITEM, Class.FINAL, retries=3, rng=Highest()) is None


def test_a_retry_keeps_where_the_item_got_to() -> None:
    item = CrawlItem("https://example.com/b", "https://example.com/b", 1, 0, 2, ("/a",))
    again = next_attempt(item, Class.ERROR, retries=1, rng=Lowest())
    assert again == (
        CrawlItem("https://example.com/b", "https://example.com/b", 1, 1, 2, ("/a",)),
        0.0,
    )


@given(st.integers(0, 10_000), st.integers(0, 10_000), st.integers(0, 10))
def test_every_item_runs_out_of_retries(attempt: int, throttled: int, retries: int) -> None:
    item = CrawlItem(ITEM.url, ITEM.key, 0, attempt, throttled)
    tries = 0
    for cls in (Class.ERROR, Class.THROTTLE):
        current: CrawlItem | None = item
        while current is not None:
            again = next_attempt(current, cls, retries=retries, rng=Lowest())
            current = again[0] if again else None
            tries += 1
    assert tries <= retries + THROTTLED_RETRIES + 2
