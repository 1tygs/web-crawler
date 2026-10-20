import asyncio
import itertools
import random
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
    RobotsFetcher,
    Triage,
    triage,
)
from web_crawler.frontier import Admission, Frontier, Hop
from web_crawler.robots import MAX_BYTES as MAX_ROBOTS_BYTES
from web_crawler.robots import Availability, RobotsTxt
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


class Sleeps:
    """Records each backoff instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class Highest(random.Random):
    """Jitter at the top of its range, so that backoffs are predictable."""

    def uniform(self, a: float, b: float) -> float:
        return b


ROBOTS = "https://example.com/robots.txt"
PLAIN_ROBOTS = "http://example.com/robots.txt"
TEXT = {"Content-Type": "text/plain"}


async def robots_txt(
    replies: Replies, origins: tuple[str, ...] = ("https://example.com",)
) -> tuple[list[RobotsTxt], FakeTransport, Sleeps]:
    transport, sleeps = FakeTransport(replies), Sleeps()
    files = await RobotsFetcher(transport, rng=Highest(), sleep=sleeps).fetch_all(origins)
    return files, transport, sleeps


async def test_robots_found() -> None:
    body = b"User-agent: *\nDisallow: /private\n"
    [file], transport, sleeps = await robots_txt({ROBOTS: Reply(headers=TEXT, body=body)})
    assert (file.url, file.availability, file.detail) == (ROBOTS, Availability.FOUND, "33 bytes")
    assert not file.allowed("https://example.com/private", "web-crawler")
    assert transport.requested == [ROBOTS]
    assert not sleeps.delays


@pytest.mark.parametrize("status", [404, 410, 401, 403, 400])
async def test_robots_unavailable_allows_everything(status: int) -> None:
    [file], transport, _ = await robots_txt({ROBOTS: Reply(status, TEXT, b"Forbidden")})
    assert file == RobotsTxt(ROBOTS, Availability.UNAVAILABLE, f"HTTP {status}")
    assert transport.requested == [ROBOTS]
    assert transport.responses[0].position == len(b"Forbidden")  # drained


@pytest.mark.parametrize(
    ("answer", "detail", "no_answer"),
    [
        (Reply(503, TEXT), "HTTP 503", None),
        (Reply(429, TEXT), "HTTP 429", None),
        (ErrorKind.CONNECT, "connect: scripted", "connect"),  # the host never answered
    ],
)
async def test_robots_unreachable_is_tried_three_times(
    answer: Answer, detail: str, no_answer: str | None
) -> None:
    [file], _, sleeps = await robots_txt({ROBOTS: answer})
    assert file == RobotsTxt(
        ROBOTS, Availability.UNREACHABLE, f"{detail}, 3 tries", no_answer=no_answer
    )
    assert sleeps.delays == [0.5, 1.0]  # full jitter, at its highest


async def test_robots_found_on_a_retry() -> None:
    replies: Replies = {ROBOTS: [Reply(503, TEXT), ErrorKind.RESET, Reply(headers=TEXT)]}
    [file], transport, sleeps = await robots_txt(replies)
    assert file.availability is Availability.FOUND
    assert transport.requested == [ROBOTS, ROBOTS]  # the reset gave no response
    assert sleeps.delays == [0.5, 1.0]


def redirects(count: int, start: str = ROBOTS) -> Replies:
    """A chain of count redirects from start, through other hosts, to the file."""
    hops = [start] + [f"https://cdn{n}.example.net/robots.txt" for n in range(count)]
    replies: Replies = {a: Reply(301, {"Location": b}) for a, b in itertools.pairwise(hops)}
    replies[hops[-1]] = Reply(headers=TEXT, body=b"User-agent: *\nDisallow: /\n")
    return replies


async def test_robots_follows_five_redirects_across_hosts() -> None:
    [file], transport, _ = await robots_txt(redirects(5))
    assert file.availability is Availability.FOUND
    assert file.url == ROBOTS  # its rules govern the origin it was asked for
    assert not file.allowed("https://example.com/", "web-crawler")
    assert len(transport.requested) == 6


async def test_robots_after_more_than_five_redirects_is_unavailable() -> None:
    [file], transport, _ = await robots_txt(redirects(6))
    assert file == RobotsTxt(ROBOTS, Availability.UNAVAILABLE, "more than 5 redirects")
    assert len(transport.requested) == 6


@pytest.mark.parametrize("location", ["http://exa mple.com/", "ftp://example.com/robots.txt"])
async def test_robots_redirect_to_a_url_that_cant_be_fetched(location: str) -> None:
    [file], _, _ = await robots_txt({ROBOTS: Reply(302, {"Location": location})})
    assert file == RobotsTxt(
        ROBOTS, Availability.UNAVAILABLE, "HTTP 302 to a URL that can't be fetched"
    )


async def test_robots_reads_at_most_500_kib() -> None:
    rule = b"Disallow: /x\n"
    body = b"User-agent: *\n" + rule * (MAX_ROBOTS_BYTES // len(rule) + 10)
    [file], transport, _ = await robots_txt({ROBOTS: Reply(headers=TEXT, body=body)})
    assert file.detail.endswith(", truncated")
    assert transport.responses[0].position == MAX_ROBOTS_BYTES


async def test_robots_requests_each_url_once_per_round() -> None:
    # The http origin's robots.txt redirects to the https one's, as most sites' do.
    replies: Replies = {
        PLAIN_ROBOTS: Reply(301, {"Location": ROBOTS}, delay=0.001),
        ROBOTS: Reply(headers=TEXT, body=b"User-agent: *\nDisallow: /x\n", delay=0.01),
    }
    origins = ("https://example.com", "http://example.com")
    transport, sleeps = FakeTransport(replies), Sleeps()
    fetcher = RobotsFetcher(transport, rng=Highest(), sleep=sleeps)
    https, http = await fetcher.fetch_all(origins)
    assert transport.requested == [PLAIN_ROBOTS, ROBOTS]
    assert (https.url, http.url) == (ROBOTS, PLAIN_ROBOTS)
    assert not http.allowed("http://example.com/x", "web-crawler")
    await fetcher.fetch_all(origins)  # a later round asks again
    assert sorted(transport.requested) == sorted([PLAIN_ROBOTS, ROBOTS] * 2)


async def test_cancelling_a_robots_round_leaves_nothing_behind() -> None:
    replies: Replies = {
        PLAIN_ROBOTS: Reply(301, {"Location": ROBOTS}),
        ROBOTS: [Reply(headers=TEXT, delay=60), Reply(headers=TEXT)],
    }
    transport = FakeTransport(replies)
    fetcher = RobotsFetcher(transport, rng=Highest(), sleep=Sleeps())
    task = asyncio.create_task(fetcher.fetch_all(("https://example.com", "http://example.com")))
    for _ in range(20):  # until both wait for the https file
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    files = await fetcher.fetch_all(("https://example.com",))
    assert files[0].availability is Availability.FOUND
