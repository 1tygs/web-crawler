import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from web_crawler.frontier import Admission, Frontier, Hop
from web_crawler.robots import RobotsPolicy, RobotsTxt
from web_crawler.types import CrawlItem
from web_crawler.urls import Scope

SCOPE = Scope.for_url("https://example.com/")
NOW = 100.0  # the clock's reading, for tests where no retry is waiting


def make() -> tuple[Frontier, Admission]:
    frontier = Frontier()
    return frontier, Admission(frontier, SCOPE)


def pop_all(frontier: Frontier, now: float = NOW) -> list[CrawlItem]:
    items = []
    while (popped := frontier.pop(now)) is not None:
        items.append(popped)
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
    assert frontier.pop(NOW) == CrawlItem("d1a", "d1a", 1)
    admission.admit([("d0", "d0")], depth=0)  # shallower than anything left
    assert [item.key for item in pop_all(frontier)] == ["d0", "d1b", "d2"]
    assert frontier.pop(NOW) is None


def test_outstanding_counts_items_until_they_finish() -> None:
    frontier, admission = make()
    admission.admit([("a", "a"), ("b", "b")], depth=0)
    assert frontier.outstanding == 2
    frontier.pop(NOW)
    assert frontier.outstanding == 2  # popped, but not finished
    frontier.finish()
    assert frontier.outstanding == 1


def item(key: str, depth: int = 1) -> CrawlItem:
    return CrawlItem(key, key, depth)


def test_a_retry_waits_until_it_is_due_and_stays_outstanding() -> None:
    frontier, admission = make()
    admission.admit([("a", "a")], depth=1)
    a = frontier.pop(NOW)
    assert a is not None
    frontier.schedule_retry(a, due=NOW + 2.0)
    assert frontier.outstanding == 1
    assert (frontier.retrying, frontier.next_retry_at()) == (1, NOW + 2.0)
    assert frontier.pop(NOW + 1.9) is None
    assert frontier.pop(NOW + 2.0) is a
    assert (frontier.retrying, frontier.next_retry_at()) == (0, None)
    assert frontier.outstanding == 1  # until it finishes


def test_a_retry_that_is_due_goes_to_the_front_of_its_depth() -> None:
    # It has waited its turn already; it doesn't go ahead of shallower items, though.
    frontier, admission = make()
    admission.admit([("d1", "d1")], depth=1)
    admission.admit([("d2a", "d2a"), ("d2b", "d2b")], depth=2)
    frontier.schedule_retry(item("late", depth=2), due=NOW + 1)
    frontier.schedule_retry(item("early", depth=2), due=NOW)
    frontier.schedule_retry(item("same time, after", depth=2), due=NOW)
    assert [i.key for i in pop_all(frontier, NOW + 5)] == [
        "d1",
        "early",
        "same time, after",
        "late",
        "d2a",
        "d2b",
    ]


def test_a_retry_at_a_shallower_depth_is_dispatched_first() -> None:
    frontier, admission = make()
    admission.admit([("d3", "d3")], depth=3)
    assert frontier.pop(NOW) == item("d3", depth=3)
    frontier.schedule_retry(item("d1", depth=1), due=NOW)
    admission.admit([("d4", "d4")], depth=4)
    assert [i.key for i in pop_all(frontier, NOW)] == ["d1", "d4"]


@given(st.data())
def test_a_crawl_finishes_each_key_once_and_ends_when_done(data: st.DataObject) -> None:
    # Pages link to random keys; fetches complete in random order, and some fail and are
    # tried again later. Every key reached finishes exactly once, a key is dispatched again
    # only after a retry, and outstanding is 0 only when nothing is queued, in flight or
    # waiting to be tried again.
    frontier, admission = make()
    pages = data.draw(st.integers(1, 30))
    page_links = st.lists(st.integers(0, pages - 1), max_size=6)
    links = data.draw(st.lists(page_links, min_size=pages, max_size=pages))
    admission.admit([("0", "0")], depth=0)
    now = 0.0
    in_flight: list[CrawlItem] = []
    dispatched: list[str] = []
    retried: list[str] = []
    finished: list[str] = []
    while frontier.outstanding:
        now += data.draw(st.sampled_from([0.0, 0.5, 2.0]))
        dispatch = not in_flight or data.draw(st.booleans())
        if dispatch and (popped := frontier.pop(now)) is not None:
            in_flight.append(popped)
            dispatched.append(popped.key)
        elif not in_flight:  # nothing to do until a retry is due
            due = frontier.next_retry_at()
            assert due is not None
            now = due
        else:  # complete one of the fetches in flight, in any order
            done = in_flight.pop(data.draw(st.integers(0, len(in_flight) - 1)))
            # Sometimes it fails, and is tried again: a key at most twice.
            if retried.count(done.key) < 2 and data.draw(st.integers(0, 3)) == 3:
                frontier.schedule_retry(done, due=now + data.draw(st.sampled_from([0.0, 1.0])))
                retried.append(done.key)
            else:
                admission.admit([(str(n), str(n)) for n in links[int(done.key)]], done.depth + 1)
                frontier.finish()
                finished.append(done.key)
        waiting = frontier.retrying
        assert frontier.outstanding == len(admission.seen) - len(finished)
        assert frontier.outstanding >= len(in_flight) + waiting
    assert sorted(finished) == sorted(admission.seen)
    assert sorted(dispatched) == sorted(finished + retried)
    assert frontier.pop(math.inf) is None
    assert frontier.next_retry_at() is None


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
    assert frontier.pop(NOW) is None


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
