import pytest

from web_crawler.stats import Stats


def test_summary_of_a_crawl() -> None:
    stats = Stats(started=100.0, pages=1234, links_found=56789, not_html=3, internal_errors=1)
    stats.statuses.update({200: 1237, 404: 2, 301: 5})
    stats.failures.update({"HTTP 404": 2, "connect": 1})
    stats.retries.update({"reset": 3, "HTTP 502": 1, "connect": 3})
    stats.body_bytes, stats.decoded_bytes = 23_456_789, 98_765_432
    assert stats.summary(unique_urls=2000, now=110.0).splitlines() == [
        "1,234 pages in 10.0 s (123.4 pages/s), 56,789 links found, 2,000 unique in-scope URLs",
        "statuses: 200 (1,237), 301 (5), 404 (2)",
        "page bodies: 23.5 MB received, 98.8 MB decompressed",
        "not printed: not HTML (3), HTTP 404 (2), connect (1)",
        "retried: HTTP 502 (1), connect (3), reset (3)",
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
