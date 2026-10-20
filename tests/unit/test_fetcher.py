from collections.abc import Collection

import pytest

from tests.support.fake_transport import Answer, FakeTransport, Reply
from web_crawler.fetcher import (
    DRAIN_LIMIT,
    MAX_REDIRECTS,
    AdmissionHops,
    Claimed,
    Fetcher,
    Final,
    NotPage,
    Page,
    Triage,
    triage,
)
from web_crawler.frontier import Admission, Frontier, Hop
from web_crawler.stats import Stats
from web_crawler.transport import ErrorKind
from web_crawler.types import CrawlItem, ParseJob
from web_crawler.urls import Scope, dedupe_key

START = "https://example.com/"
HTML = {"Content-Type": "text/html; charset=utf-8"}

type Replies = dict[str, Answer | list[Answer]]


def item(url: str = START) -> CrawlItem:
    return CrawlItem(url=url, key=dedupe_key(url), depth=0)


def claiming(*keys: str) -> Admission:
    """Admission for START's host, with keys already claimed by other items."""
    admission = Admission(Frontier(), Scope.for_url(START))
    admission.seen.update(keys)
    return admission


async def fetch(
    replies: Replies,
    url: str = START,
    max_page_bytes: int = 1 << 20,
    admission: Admission | None = None,
) -> tuple[object, FakeTransport, Stats]:
    transport = FakeTransport(replies)
    stats = Stats(started=0.0)
    admission = admission or claiming()
    admission.seen.add(dedupe_key(url))  # as the item's own admission did
    fetcher = Fetcher(transport, max_page_bytes, stats)
    return await fetcher.fetch(item(url), AdmissionHops(admission)), transport, stats


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
    replies: Replies = {
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
    assert outcome == Final(
        "redirect out of scope",
        f"HTTP 301 to {location}, outside the crawl's scope",
        (START,),
        301,
    )
    assert transport.requested == [START]


async def test_claims_each_redirect_target_before_requesting_it() -> None:
    admission = claiming()
    replies: Replies = {
        START: Reply(301, {"Location": "/a"}),
        "https://example.com/a": Reply(302, {"Location": "/%7eb"}),
        "https://example.com/%7eb": Reply(headers=HTML),
    }
    outcome, _, _ = await fetch(replies, admission=admission)
    assert isinstance(outcome, Page)
    assert admission.seen == {START, "https://example.com/a", "https://example.com/~b"}


@pytest.mark.parametrize(
    ("location", "target"),
    [
        ("/b", "https://example.com/b"),
        ("https://example.com/b#top", "https://example.com/b"),
        ("/%62", "https://example.com/%62"),  # the same resource, by its dedupe key
    ],
)
async def test_stops_at_a_redirect_to_a_url_another_item_claimed(
    location: str, target: str
) -> None:
    # That item prints the page; following the redirect would fetch and print it twice.
    replies: Replies = {START: Reply(301, {"Location": location}, body=b"moved")}
    outcome, transport, _ = await fetch(replies, admission=claiming("https://example.com/b"))
    assert outcome == Claimed(target, (START,))
    assert transport.requested == [START]
    assert transport.responses[0].position == len(b"moved")  # drained


async def test_follows_a_cookie_gate_back_to_where_it_started() -> None:
    # The page redirects to one that sets a cookie and redirects back; now it's served.
    gate = "https://example.com/login?next=/"
    replies: Replies = {
        START: [Reply(302, {"Location": "/login?next=/"}), Reply(headers=HTML, body=b"page")],
        gate: Reply(302, {"Location": "/"}),
    }
    outcome, transport, _ = await fetch(replies)
    assert outcome == Page(ParseJob(START, b"page", "utf-8"))
    assert transport.requested == [START, gate, START]


async def test_a_redirect_loop_ends_at_the_hop_limit() -> None:
    replies: Replies = {
        START: Reply(302, {"Location": "/a"}),
        "https://example.com/a": Reply(302, {"Location": "/"}),
    }
    outcome, transport, _ = await fetch(replies)
    chain = tuple([START, "https://example.com/a"] * 6)[: 1 + MAX_REDIRECTS]
    assert outcome == Final(
        "redirect loop",
        f"redirects in a loop through {START}, https://example.com/a",
        chain,
        302,
    )
    assert transport.requested == list(chain)


class Recorder:
    """A hop policy that follows everything and records what it was asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[set[str], str, str]] = []

    async def admit_hop(self, chain: Collection[str], url: str, key: str) -> Hop:
        self.asked.append((set(chain), url, key))
        return Hop.FOLLOW


async def test_asks_the_hop_policy_with_the_chain_so_far() -> None:
    replies: Replies = {
        START: Reply(301, {"Location": "/%7Ea"}),
        "https://example.com/%7Ea": Reply(301, {"Location": "https://elsewhere.org/"}),
        "https://elsewhere.org/": Reply(headers=HTML),
    }
    hops = Recorder()
    fetcher = Fetcher(FakeTransport(replies), 1 << 20, Stats(started=0.0))
    assert isinstance(await fetcher.fetch(item(), hops), Page)
    assert hops.asked == [
        ({START}, "https://example.com/%7Ea", "https://example.com/~a"),
        ({START, "https://example.com/~a"}, "https://elsewhere.org/", "https://elsewhere.org/"),
    ]


async def test_a_redirect_to_an_invalid_url() -> None:
    outcome, _, _ = await fetch({START: Reply(302, {"Location": "http://exa mple.com/"})})
    assert outcome == Final(
        "bad redirect", "HTTP 302 to 'http://exa mple.com/', which isn't a valid URL", (START,), 302
    )


async def test_gives_up_after_ten_redirects() -> None:
    replies: Replies = {
        f"https://example.com/{n}": Reply(307, {"Location": f"/{n + 1}"}) for n in range(20)
    }
    outcome, transport, _ = await fetch(replies, url="https://example.com/0")
    chain = tuple(f"https://example.com/{n}" for n in range(1 + MAX_REDIRECTS))
    assert outcome == Final(
        "too many redirects",
        "more than 10 redirects; the last was to https://example.com/11",
        chain,  # it ends with the last URL requested
        307,
    )
    assert outcome.url == f"https://example.com/{MAX_REDIRECTS}"
    assert transport.requested == list(chain)


async def test_ten_redirects_are_fine() -> None:
    replies: Replies = {
        f"https://example.com/{n}": Reply(307, {"Location": f"/{n + 1}"}) for n in range(10)
    }
    replies["https://example.com/10"] = Reply(headers=HTML)
    outcome, _, _ = await fetch(replies, url="https://example.com/0")
    assert isinstance(outcome, Page)


async def test_drains_a_redirect_so_its_connection_can_be_reused() -> None:
    replies: Replies = {
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
    assert outcome == Final(f"HTTP {status}", f"HTTP {status}", (START,), status)
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
