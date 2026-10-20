"""robots.txt rules (RFC 9309): what each file allows, and the policy over a crawl's
origins. Fetching is tested with the fetcher."""

import pytest

from web_crawler.robots import (
    MAX_BYTES,
    Availability,
    RobotsPolicy,
    RobotsTxt,
    product_token,
    unreachable,
    without_file,
)

PRODUCT = "web-crawler"
ROBOTS = "https://example.com/robots.txt"


def found(text: str, url: str = ROBOTS) -> RobotsTxt:
    return RobotsTxt.found(url, text.encode(), truncated=False)


@pytest.mark.parametrize(
    ("user_agent", "token"),
    [
        ("web-crawler/1.0 (+https://github.com/1tygs/web-crawler)", "web-crawler"),
        ("Googlebot/2.1 (+http://www.google.com/bot.html)", "Googlebot"),
        ("my_bot", "my_bot"),
        ("bot2/1.0", "bot"),  # RFC 9309 allows letters, "_" and "-" only
        ("Mozilla/5.0 (compatible; ExampleBot/1.0)", "Mozilla"),
        ("2bot", ""),
        ("", ""),
    ],
)
def test_product_token(user_agent: str, token: str) -> None:
    assert product_token(user_agent) == token


@pytest.mark.parametrize(
    ("status", "availability"),
    [
        # Unavailable: the crawler may access anything (RFC 9309 §2.3.1.3), whatever the 4xx.
        *[(status, Availability.UNAVAILABLE) for status in (400, 401, 403, 404, 410, 451)],
        (302, Availability.UNAVAILABLE),  # a redirect with nowhere to go
        (304, Availability.UNAVAILABLE),
        (600, Availability.UNAVAILABLE),
        # Unreachable: everything is disallowed (§2.3.1.4), until a retry gets through.
        *[(status, Availability.UNREACHABLE) for status in (429, 500, 502, 503, 504, 599)],
    ],
)
def test_without_file(status: int, availability: Availability) -> None:
    file = without_file(ROBOTS, status)
    assert file == RobotsTxt(ROBOTS, availability, f"HTTP {status}")
    assert unreachable(status) is (availability is Availability.UNREACHABLE)
    assert file.allowed("https://example.com/x", PRODUCT) is (
        availability is Availability.UNAVAILABLE
    )
    assert file.crawl_delay(PRODUCT) is None


RULES = """
User-agent: *
Disallow: /private/
Allow: /private/open
Disallow: /*.pdf$
Disallow: /search?

User-agent: other-bot
Disallow: /
"""


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/", True),
        ("/public", True),
        ("/private/", False),
        ("/private/secret", False),
        ("/private/open", True),  # the longest match wins (RFC 9309 §2.2.2)
        ("/private/opener", True),
        ("/file.pdf", False),  # * and $
        ("/file.pdf?x=1", True),
        ("/search?q=a", False),
        ("/search", True),
        ("/%70rivate/", False),  # an escape of an unreserved character is the character
        ("/robots.txt", True),  # implicitly allowed (§2.2.2)
    ],
)
def test_found_rules(path: str, allowed: bool) -> None:
    file = found(RULES)
    assert file.availability is Availability.FOUND
    assert file.allowed(f"https://example.com{path}", PRODUCT) is allowed


@pytest.mark.parametrize(
    ("groups", "allowed"),
    [
        # The group that names the product token, in any case, wins over "*".
        ("User-agent: Web-Crawler\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n", (False, True)),
        ("User-agent: *\nDisallow: /b\n", (True, False)),
        ("User-agent: other\nDisallow: /a\n", (True, True)),
        # Protego is looser than RFC 9309: a group named after a token's start applies too,
        # here "web" to web-crawler (DESIGN.md A.6). Pinned, not worked around.
        ("User-agent: web\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n", (False, True)),
        ("User-agent: crawler\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n", (True, False)),
    ],
)
def test_groups_match_on_the_product_token(groups: str, allowed: tuple[bool, bool]) -> None:
    file = found(groups)
    assert (
        file.allowed("https://example.com/a", PRODUCT),
        file.allowed("https://example.com/b", PRODUCT),
    ) == allowed


def test_a_crawler_without_a_product_token_obeys_the_star_group() -> None:
    file = found("User-agent: web-crawler\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n")
    assert file.allowed("https://example.com/a", "")
    assert not file.allowed("https://example.com/b", "")


def test_a_byte_order_mark_does_not_hide_the_first_line() -> None:
    file = RobotsTxt.found(ROBOTS, b"\xef\xbb\xbfUser-agent: *\nDisallow: /x\n", truncated=False)
    assert not file.allowed("https://example.com/x", PRODUCT)


def test_a_file_that_isnt_utf8_is_read_with_replacement() -> None:
    body = b"User-agent: *\nDisallow: /caf\xe9\nDisallow: /x\n"
    file = RobotsTxt.found(ROBOTS, body, truncated=False)
    assert not file.allowed("https://example.com/x", PRODUCT)


def test_a_truncated_file_drops_its_cut_off_line() -> None:
    body = b"User-agent: *\nDisallow: /private\nDisallow: /p"  # "/p" was "/photos" whole
    file = RobotsTxt.found(ROBOTS, body, truncated=True)
    assert file.detail == "33 bytes, truncated"
    assert file.allowed("https://example.com/public", PRODUCT)
    assert not file.allowed("https://example.com/private", PRODUCT)
    assert found("User-agent: *\n").detail == "14 bytes"
    assert MAX_BYTES == 500 * 1024


def test_crawl_delay() -> None:
    file = found("User-agent: *\nCrawl-delay: 2\n\nUser-agent: web-crawler\nCrawl-delay: 3.5\n")
    assert file.crawl_delay(PRODUCT) == 3.5
    assert file.crawl_delay("other") == 2


def test_policy_applies_each_origins_file() -> None:
    policy = RobotsPolicy(
        PRODUCT,
        {
            "https://example.com": found("User-agent: *\nDisallow: /secure-only\n"),
            "http://example.com": found("User-agent: *\nDisallow: /plain-only\n"),
        },
    )
    assert not policy.allowed("https://example.com/secure-only")
    assert policy.allowed("https://example.com/plain-only")
    assert not policy.allowed("http://example.com/plain-only")
    assert policy.allowed("http://example.com/secure-only")
    assert policy.file_for("https://example.com:8443/") is None
    assert policy.allowed("https://example.com.evil.net/plain-only")  # no file governs it


def test_policy_crawl_delay_is_the_longest() -> None:
    files = {
        "https://example.com": found("User-agent: *\nCrawl-delay: 1\n"),
        "http://example.com": found("User-agent: *\nCrawl-delay: 4\n"),
    }
    assert RobotsPolicy(PRODUCT, files).crawl_delay == 4
    assert RobotsPolicy(PRODUCT, {"https://example.com": found("")}).crawl_delay is None


def test_a_policy_without_files_allows_everything() -> None:
    policy = RobotsPolicy(PRODUCT, {})
    assert policy.allowed("https://example.com/anything")
    assert policy.crawl_delay is None
