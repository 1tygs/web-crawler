from web_crawler.stats import Stats


def test_summary_of_a_crawl() -> None:
    stats = Stats(started=100.0, pages=1234, links_found=56789, not_html=3, internal_errors=1)
    stats.statuses.update({200: 1237, 404: 2, 301: 5})
    stats.failures.update({"HTTP 404": 2, "connect": 1})
    assert stats.summary(unique_urls=2000, now=110.0).splitlines() == [
        "1,234 pages in 10.0 s (123.4 pages/s), 56,789 links found, 2,000 unique in-scope URLs",
        "statuses: 200 (1,237), 301 (5), 404 (2)",
        "not printed: not HTML (3), HTTP 404 (2), connect (1)",
        "internal errors: 1, logged above",
    ]


def test_summary_of_an_empty_crawl() -> None:
    assert Stats(started=5.0).summary(unique_urls=0, now=5.0) == (
        "0 pages in 0.0 s (0.0 pages/s), 0 links found, 0 unique in-scope URLs"
    )


def test_summary_counts_one_in_the_singular() -> None:
    stats = Stats(started=0.0, pages=1, links_found=1)
    assert stats.summary(unique_urls=1, now=2.0) == (
        "1 page in 2.0 s (0.5 pages/s), 1 link found, 1 unique in-scope URL"
    )
