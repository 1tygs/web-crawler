import pytest

from tests.support.fake_transport import FakeTransport, Reply
from web_crawler.fetcher import (
    DRAIN_LIMIT,
    MAX_REDIRECTS,
    Fetcher,
    Final,
    NotPage,
    Page,
    Triage,
    triage,
)
from web_crawler.stats import Stats
from web_crawler.transport import ErrorKind
from web_crawler.types import CrawlItem, ParseJob
from web_crawler.urls import Scope

START = "https://example.com/"
HTML = {"Content-Type": "text/html; charset=utf-8"}


def item(url: str = START) -> CrawlItem:
    return CrawlItem(url=url, key=url, depth=0)


async def fetch(
    replies: dict[str, Reply | ErrorKind], url: str = START, max_page_bytes: int = 1 << 20
) -> tuple[object, FakeTransport, Stats]:
    transport = FakeTransport(replies)
    stats = Stats(started=0.0)
    fetcher = Fetcher(transport, Scope.for_url(START), max_page_bytes, stats)
    return await fetcher.fetch(item(url)), transport, stats


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (200, HTML, Triage.PAGE),
        (200, {"Content-Type": "application/xhtml+xml"}, Triage.PAGE),
        (203, {"Content-Type": "TEXT/HTML"}, Triage.PAGE),
        (200, {"Content-Type": "text/plain"}, Triage.NOT_PAGE),
        (200, {"Content-Type": "text/html-sandboxed"}, Triage.NOT_PAGE),
        (200, {}, Triage.NOT_PAGE),  # sniffing a missing type comes later
        (204, {}, Triage.NOT_PAGE),
        (301, {"Location": "/x"}, Triage.REDIRECT),
        (302, {"Location": "/x"}, Triage.REDIRECT),
        (303, {"Location": "/x"}, Triage.REDIRECT),
        (307, {"Location": "/x"}, Triage.REDIRECT),
        (308, {"Location": "/x"}, Triage.REDIRECT),
        (301, HTML, Triage.FINAL),  # a redirect with nowhere to go
        (300, {"Location": "/x"}, Triage.FINAL),
        (304, {"Location": "/x"}, Triage.FINAL),
        (404, HTML, Triage.FINAL),
        (500, HTML, Triage.FINAL),
    ],
)
def test_triage(status: int, headers: dict[str, str], expected: Triage) -> None:
    assert triage(status, headers) is expected


async def test_a_page() -> None:
    outcome, transport, stats = await fetch({START: Reply(headers=HTML, body=b"<p>hi")})
    assert outcome == Page(ParseJob(url=START, body=b"<p>hi", http_charset="utf-8"))
    assert transport.requested == [START]
    assert stats.statuses == {200: 1}


async def test_reads_a_page_up_to_the_cap() -> None:
    outcome, _, _ = await fetch({START: Reply(headers=HTML, body=b"x" * 100)}, max_page_bytes=10)
    assert outcome == Page(ParseJob(START, b"x" * 10, "utf-8", truncated=True))
    outcome, _, _ = await fetch({START: Reply(headers=HTML, body=b"x" * 10)}, max_page_bytes=10)
    assert outcome == Page(ParseJob(START, b"x" * 10, "utf-8", truncated=False))


@pytest.mark.parametrize(
    ("content_type", "charset"),
    [
        ("text/html; charset=Shift_JIS", "Shift_JIS"),
        ('text/html; charset="koi8-r"', "koi8-r"),
        ("text/html", None),
        ("text/html; charset=", None),
    ],
)
async def test_passes_the_headers_charset_to_the_parser(
    content_type: str, charset: str | None
) -> None:
    outcome, _, _ = await fetch({START: Reply(headers={"Content-Type": content_type})})
    assert isinstance(outcome, Page)
    assert outcome.job.http_charset == charset


async def test_follows_redirects_and_reports_the_final_url() -> None:
    replies: dict[str, Reply | ErrorKind] = {
        START: Reply(301, {"Location": "/a#fragment"}),
        "https://example.com/a": Reply(302, {"Location": "http://example.com/b"}),
        "http://example.com/b": Reply(headers=HTML, body=b"page"),
    }
    outcome, transport, stats = await fetch(replies)
    assert outcome == Page(ParseJob("http://example.com/b", b"page", http_charset="utf-8"))
    assert transport.requested == [START, "https://example.com/a", "http://example.com/b"]
    assert stats.statuses == {301: 1, 302: 1, 200: 1}


@pytest.mark.parametrize(
    "location",
    ["https://www.example.com/", "https://example.com:8443/", "https://elsewhere.org/"],
)
async def test_does_not_follow_a_redirect_out_of_scope(location: str) -> None:
    outcome, transport, _ = await fetch({START: Reply(301, {"Location": location})})
    assert isinstance(outcome, Final)
    assert outcome.reason == "redirect out of scope"
    assert location in outcome.detail
    assert transport.requested == [START]


async def test_a_redirect_to_an_invalid_url() -> None:
    outcome, _, _ = await fetch({START: Reply(302, {"Location": "http://exa mple.com/"})})
    assert isinstance(outcome, Final)
    assert outcome.reason == "bad redirect"


async def test_gives_up_after_ten_redirects() -> None:
    replies: dict[str, Reply | ErrorKind] = {
        f"https://example.com/{n}": Reply(307, {"Location": f"/{n + 1}"}) for n in range(20)
    }
    outcome, transport, _ = await fetch(replies, url="https://example.com/0")
    assert outcome == Final(
        "too many redirects",
        "more than 10 redirects; the last was to https://example.com/11",
        f"https://example.com/{MAX_REDIRECTS}",  # the last URL requested
    )
    assert len(transport.requested) == 1 + MAX_REDIRECTS


async def test_ten_redirects_are_fine() -> None:
    replies: dict[str, Reply | ErrorKind] = {
        f"https://example.com/{n}": Reply(307, {"Location": f"/{n + 1}"}) for n in range(10)
    }
    replies["https://example.com/10"] = Reply(headers=HTML)
    outcome, _, _ = await fetch(replies, url="https://example.com/0")
    assert isinstance(outcome, Page)


async def test_drains_a_redirect_so_its_connection_can_be_reused() -> None:
    replies: dict[str, Reply | ErrorKind] = {
        START: Reply(301, {"Location": "/a"}, body=b"moved"),
        "https://example.com/a": Reply(headers=HTML),
    }
    _, transport, _ = await fetch(replies)
    assert transport.responses[0].position == len(b"moved")


@pytest.mark.parametrize(
    ("length", "read"),
    [(str(DRAIN_LIMIT), DRAIN_LIMIT), (str(DRAIN_LIMIT + 1), 0), (None, 0), ("junk", 0)],
)
async def test_drains_a_small_non_html_body_and_drops_a_large_one(
    length: str | None, read: int
) -> None:
    headers = {"Content-Type": "image/png"} | ({"Content-Length": length} if length else {})
    body = b"x" * (DRAIN_LIMIT + 1 if length == str(DRAIN_LIMIT + 1) else DRAIN_LIMIT)
    outcome, transport, _ = await fetch({START: Reply(headers=headers, body=body)})
    assert outcome == NotPage(START, "image/png")
    assert transport.responses[0].position == read


@pytest.mark.parametrize("status", [404, 410, 403, 500, 503])
async def test_an_error_status_is_final(status: int) -> None:
    outcome, _, stats = await fetch({START: Reply(status, HTML)})
    assert outcome == Final(f"HTTP {status}", f"HTTP {status}", START)
    assert stats.statuses == {status: 1}


@pytest.mark.parametrize("kind", [ErrorKind.CONNECT, ErrorKind.DNS, ErrorKind.TIMEOUT])
async def test_a_transport_error_is_final(kind: ErrorKind) -> None:
    outcome, _, stats = await fetch({START: kind})
    assert isinstance(outcome, Final)
    assert outcome.reason == str(kind)
    assert outcome.url == START
    assert not stats.statuses


async def test_a_body_cut_off_is_final() -> None:
    outcome, _, _ = await fetch({START: Reply(headers=HTML, read_error=ErrorKind.PAYLOAD)})
    assert isinstance(outcome, Final)
    assert outcome.reason == "payload"
