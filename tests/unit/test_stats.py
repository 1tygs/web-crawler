import math
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from web_crawler.stats import Histogram, Snapshot, Stats


def test_summary_of_a_crawl() -> None:
    stats = Stats(started=100.0, pages=1234, links_found=56789, not_html=3, internal_errors=1)
    stats.statuses.update({200: 1237, 404: 2, 301: 5})
    stats.failures.update({"HTTP 404": 2, "connect": 1})
    stats.retries.update({"reset": 3, "HTTP 502": 1, "connect": 3})
    stats.body_bytes, stats.decoded_bytes = 23_456_789, 98_765_432
    for ttfb in [0.02] * 50 + [0.1] * 45 + [0.4] * 4 + [1.5]:
        stats.ttfb.add(ttfb)
    stats.busiest_second = 210
    for limit in (8, 16, 32, 64, 32, 33):
        stats.limit(limit)
    summary = stats.summary(
        unique_urls=2000,
        now=110.0,
        rejected={"robots.txt": 3},
        examples={"robots.txt": ["https://example.com/private"]},
        peak_rss=70_543_360,
    )
    assert summary.splitlines() == [
        "1,234 pages in 10.0 s (123.4 pages/s), 56,789 links found, 2,000 unique in-scope URLs",
        "statuses: 200 (1,237), 301 (5), 404 (2)",
        "time to first byte: p50 20.0 ms, p95 100.3 ms, p99 399.8 ms",
        "page bodies: 23.5 MB received, 98.8 MB decompressed",
        "not printed: not HTML (3), HTTP 404 (2), connect (1)",
        "retried: HTTP 502 (1), connect (3), reset (3)",
        "not crawled: robots.txt (3, such as https://example.com/private)",
        "busiest second: 210 pages",
        "concurrency limit: 8 to 64, 33 at the end",
        "peak RSS: 67.3 MiB, parser processes aside",
        "internal errors: 1, logged above",
    ]


def test_summary_counts_pages_cut_short() -> None:
    stats = Stats(started=0.0, pages=3, body_bytes=2000, decoded_bytes=9000, truncated=2)
    assert stats.summary(unique_urls=3, now=1.0).splitlines()[1] == (
        "page bodies: 2.0 kB received, 9.0 kB decompressed, 2 cut short"
    )


@pytest.mark.parametrize(
    ("received", "decompressed", "line"),
    [
        (1, 0, "page bodies: 1 B received, 0 B decompressed"),
        (999, 1000, "page bodies: 999 B received, 1.0 kB decompressed"),
        (999_949, 1_000_000, "page bodies: 999.9 kB received, 1.0 MB decompressed"),
        (
            12_345_678_901,
            1_234_567_890_123,
            "page bodies: 12.3 GB received, 1,234.6 GB decompressed",
        ),
    ],
)
def test_summary_counts_bytes_in_decimal_units(received: int, decompressed: int, line: str) -> None:
    stats = Stats(started=0.0, pages=1, body_bytes=received, decoded_bytes=decompressed)
    assert stats.summary(unique_urls=1, now=1.0).splitlines()[1] == line


def test_summary_of_an_empty_crawl() -> None:
    assert Stats(started=5.0).summary(unique_urls=0, now=5.0) == (
        "0 pages in 0.0 s (0.0 pages/s), 0 links found, 0 unique in-scope URLs"
    )


def test_summary_counts_one_in_the_singular() -> None:
    stats = Stats(started=0.0, pages=1, links_found=1)
    assert stats.summary(unique_urls=1, now=2.0) == (
        "1 page in 2.0 s (0.5 pages/s), 1 link found, 1 unique in-scope URL"
    )


def test_a_limit_that_never_changed_is_one_number() -> None:
    stats = Stats(started=0.0)
    stats.limit(3)
    stats.limit(3)
    assert stats.summary(unique_urls=1, now=1.0).splitlines()[-1] == "concurrency limit: 3"


def test_a_time_to_first_byte_of_a_second_or_more_is_in_seconds() -> None:
    stats = Stats(started=0.0)
    stats.ttfb.add(2.5)
    assert stats.summary(unique_urls=1, now=1.0).splitlines()[1] == (
        "time to first byte: p50 2.49 s, p95 2.49 s, p99 2.49 s"
    )


def test_the_busiest_second_counts_pages_printed_in_each_second_of_the_crawl() -> None:
    stats = Stats(started=10.0)
    for now in [10.1, 10.9, 11.0, 11.2, 11.3, 11.99, 13.5]:
        stats.printed(now, links_found=2, decoded_bytes=100, truncated=now > 13)
    assert stats.busiest_second == 4  # from 11 to 12
    assert (stats.pages, stats.links_found, stats.decoded_bytes, stats.truncated) == (
        7,
        14,
        700,
        1,
    )


def nearest_rank(values: list[float], q: float) -> float:
    return sorted(values)[max(1, math.ceil(q * len(values))) - 1]


@given(
    st.lists(st.floats(1e-4, 600, allow_nan=False), min_size=1, max_size=300),
    st.sampled_from([0.01, 0.5, 0.95, 0.99, 1.0]),
)
def test_a_quantile_is_within_half_a_percent_of_the_exact_one(
    values: list[float], q: float
) -> None:
    histogram = Histogram()
    for value in values:
        histogram.add(value)
    assert histogram.count == len(values)
    estimate = histogram.quantile(q)
    assert estimate is not None
    assert estimate == pytest.approx(nearest_rank(values, q), rel=0.005)


def test_a_histogram_holds_a_bucket_for_each_percent_of_range_it_has_seen() -> None:
    # 100,000 log-normal times to first byte, as a crawl sees them, take a few hundred
    # buckets, whatever their number.
    rng = random.Random(1)
    values = [rng.lognormvariate(math.log(0.1), 1.0) for _ in range(100_000)]
    histogram = Histogram()
    for value in values:
        histogram.add(value)
    assert len(histogram._counts) < 1500
    for q in (0.5, 0.95, 0.99):
        assert histogram.quantile(q) == pytest.approx(nearest_rank(values, q), rel=0.005)


def test_durations_at_or_under_a_tenth_of_a_millisecond_share_the_first_bucket() -> None:
    histogram = Histogram()
    assert histogram.quantile(0.5) is None
    for value in (0.0, 1e-6, 1e-4):
        histogram.add(value)
    assert histogram.quantile(1.0) == pytest.approx(1e-4, rel=0.005)


def test_the_progress_line() -> None:
    snapshot = Snapshot(
        pages=48_210,
        rate=431.4,
        in_flight=64,
        limit=64,
        queued=12_904,
        retrying=3,
        errors=17,
        byte_rate=7_612_345.6,
        busy=2,
        workers=9,
    )
    assert snapshot.line() == (
        "pages 48,210 · 431/s · in-flight 64/64 · queued 12,904 · retrying 3 · errors 17 · "
        "7.6 MB/s · workers 2/9"
    )


def test_the_progress_line_parsing_inline_and_paused() -> None:
    snapshot = Snapshot(
        pages=3,
        rate=0.0,
        in_flight=0,
        limit=8,
        queued=40,
        retrying=0,
        errors=0,
        byte_rate=0.0,
        busy=0,
        workers=0,
        paused="the host isn't answering",
    )
    assert snapshot.line() == (
        "pages 3 · 0/s · in-flight 0/8 · queued 40 · retrying 0 · errors 0 · 0 B/s · "
        "paused: the host isn't answering"
    )
