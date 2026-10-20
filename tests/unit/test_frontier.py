import pytest
from hypothesis import given
from hypothesis import strategies as st

from web_crawler.frontier import Admission, Frontier, Hop
from web_crawler.robots import RobotsPolicy, RobotsTxt
from web_crawler.types import CrawlItem
from web_crawler.urls import Scope

SCOPE = Scope.for_url("https://example.com/")


def make() -> tuple[Frontier, Admission]:
    frontier = Frontier()
    return frontier, Admission(frontier, SCOPE)


def pop_all(frontier: Frontier) -> list[CrawlItem]:
    items = []
    while (item := frontier.pop()) is not None:
        items.append(item)
    return items


def test_admits_each_key_once() -> None:
    frontier, admission = make()
    assert admission.admit([("a", "A"), ("b", "B"), ("a", "A2")], depth=1) == 2
    assert admission.admit([("b", "B"), ("c", "C")], depth=2) == 1
    assert admission.seen == {"a", "b", "c"}
    assert [(item.key, item.url, item.depth) for item in pop_all(frontier)] == [
        ("a", "A", 1),
        ("b", "B", 1),
        ("c", "C", 2),
    ]


def test_dispatches_the_shallowest_first_in_fifo_order() -> None:
    frontier, admission = make()
    admission.admit([("d2", "d2")], depth=2)
    admission.admit([("d1a", "d1a"), ("d1b", "d1b")], depth=1)
    assert frontier.pop() == CrawlItem("d1a", "d1a", 1)
    admission.admit([("d0", "d0")], depth=0)  # shallower than anything left
    assert [item.key for item in pop_all(frontier)] == ["d0", "d1b", "d2"]
    assert frontier.pop() is None


def test_outstanding_counts_items_until_they_finish() -> None:
    frontier, admission = make()
    admission.admit([("a", "a"), ("b", "b")], depth=0)
    assert frontier.outstanding == 2
    frontier.pop()
    assert frontier.outstanding == 2  # popped, but not finished
    frontier.finish()
    assert frontier.outstanding == 1


@given(st.data())
def test_a_crawl_dispatches_each_key_once_and_ends_when_done(data: st.DataObject) -> None:
    # Pages link to random keys; completions happen in random order. Every key reached is
    # dispatched exactly once, and outstanding is 0 only when nothing is queued or in flight.
    frontier, admission = make()
    pages = data.draw(st.integers(1, 30))
    page_links = st.lists(st.integers(0, pages - 1), max_size=6)
    links = data.draw(st.lists(page_links, min_size=pages, max_size=pages))
    admission.admit([("0", "0")], depth=0)
    in_flight: list[CrawlItem] = []
    dispatched: list[str] = []
    while frontier.outstanding:
        dispatch = not in_flight or data.draw(st.booleans())
        if dispatch and (item := frontier.pop()) is not None:
            in_flight.append(item)
            dispatched.append(item.key)
        else:  # complete one of the fetches in flight, in any order
            item = in_flight.pop(data.draw(st.integers(0, len(in_flight) - 1)))
            admission.admit([(str(n), str(n)) for n in links[int(item.key)]], item.depth + 1)
            frontier.finish()
        assert frontier.outstanding == len(in_flight) + len(admission.seen) - len(dispatched)
    assert sorted(dispatched) == sorted(admission.seen)
    assert len(dispatched) == len(set(dispatched))
    assert frontier.pop() is None


@pytest.mark.parametrize(
    ("url", "chain", "hop"),
    [
        # A new in-scope URL is claimed for this item, so no one else fetches it.
        ("https://example.com/new", set(), Hop.FOLLOW),
        ("http://example.com/new", set(), Hop.FOLLOW),
        # One claimed by another item is that item's to print.
        ("https://example.com/claimed", set(), Hop.CLAIMED),
        # One in the item's own chain is followed again: a cookie gate coming back.
        ("https://example.com/claimed", {"https://example.com/claimed"}, Hop.FOLLOW),
        # Anything else is outside the scope, even if the chain went there.
        ("https://www.example.com/", set(), Hop.OUT_OF_SCOPE),
        ("https://www.example.com/", {"https://www.example.com/"}, Hop.OUT_OF_SCOPE),
    ],
)
def test_admit_hop(url: str, chain: set[str], hop: Hop) -> None:
    frontier, admission = make()
    admission.seen.add("https://example.com/claimed")
    assert admission.admit_hop(chain, url, url) is hop
    assert (url in admission.seen) is (hop is not Hop.OUT_OF_SCOPE)
    assert frontier.outstanding == 0  # a hop is part of its item, not an item of its own


def test_a_claimed_hop_isnt_admitted_again_as_a_link() -> None:
    frontier, admission = make()
    assert (
        admission.admit_hop(set(), "https://example.com/r", "https://example.com/r") is Hop.FOLLOW
    )
    assert admission.admit([("https://example.com/r", "https://example.com/r")], depth=1) == 0
    assert frontier.pop() is None


def disallowing(path: str) -> RobotsPolicy:
    rules = f"User-agent: *\nDisallow: {path}\n".encode()
    file = RobotsTxt.found("https://example.com/robots.txt", rules, truncated=False)
    return RobotsPolicy("web-crawler", {"https://example.com": file})


def test_admit_claims_and_counts_what_robots_txt_disallows() -> None:
    frontier = Frontier()
    admission = Admission(frontier, SCOPE, disallowing("/private"))
    private = [f"https://example.com/private/{n}" for n in range(5)]
    links = [(url, url) for url in [*private, "https://example.com/ok"]]
    assert admission.admit(links, depth=1) == 1
    assert admission.rejected == {"robots.txt": 5}
    assert admission.examples == {"robots.txt": private[:3]}
    # Found again, they're neither checked nor counted again.
    assert admission.admit(links, depth=2) == 0
    assert admission.rejected == {"robots.txt": 5}
    assert pop_all(frontier) == [CrawlItem("https://example.com/ok", "https://example.com/ok", 1)]


def test_admit_hop_to_a_url_robots_txt_disallows() -> None:
    frontier = Frontier()
    admission = Admission(frontier, SCOPE, disallowing("/private"))
    url = "https://example.com/private/x"
    assert admission.admit_hop(set(), url, url) is Hop.DISALLOWED
    assert url in admission.seen
    assert admission.rejected == {"robots.txt": 1}
    assert admission.admit_hop(set(), url, url) is Hop.CLAIMED  # checked once
