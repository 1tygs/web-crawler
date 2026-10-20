import math
from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.support.bench import bfs, make_site, simulate_depth
from web_crawler.frontier import DEPTH_LIMIT, Admission, Frontier, Hop
from web_crawler.robots import RobotsPolicy, RobotsTxt
from web_crawler.types import CrawlItem
from web_crawler.urls import (
    LONG_URL,
    QUERY_VARIANTS,
    REPEATED_SEGMENT,
    SKIPPED_EXTENSION,
    Scope,
)

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
    assert (frontier.outstanding, frontier.queued) == (2, 2)
    a = frontier.pop(NOW)
    assert a is not None
    assert (frontier.outstanding, frontier.queued) == (2, 1)  # popped, but not finished
    frontier.finish(a)
    assert (frontier.outstanding, frontier.queued) == (1, 1)


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
    assert frontier.queued == 0
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
                frontier.finish(done)
                finished.append(done.key)
        waiting = frontier.retrying
        assert frontier.outstanding == len(admission.seen) - len(finished)
        assert frontier.outstanding == frontier.queued + len(in_flight) + waiting
    assert sorted(finished) == sorted(admission.seen)
    assert sorted(dispatched) == sorted(finished + retried)
    assert frontier.pop(math.inf) is None
    assert frontier.next_retry_at() is None


def nodes(*names: object) -> list[tuple[str, str]]:
    return [(str(name), str(name)) for name in names]


def test_without_max_depth_nothing_is_lowered_or_held_back() -> None:
    frontier, admission = make()
    assert frontier.lowerable is None
    admission.admit(nodes("start"), depth=0)
    start = frontier.pop(NOW)
    assert start is not None
    admission.admit(nodes("a"), depth=1)
    frontier.finish(start)
    a = frontier.pop(NOW)
    assert a is not None
    admission.admit(nodes("b"), depth=2)
    admission.admit(nodes("c"), depth=5)  # a depth no queue had
    admission.admit(nodes("c"), depth=1)  # found again, shallower: it stays at 5
    assert [(i.key, i.depth) for i in pop_all(frontier)] == [("b", 2), ("c", 5)]


def test_an_item_found_again_shallower_is_lowered_while_queued() -> None:
    frontier = Frontier(max_depth=10)
    admission = Admission(frontier, SCOPE)
    admission.admit(nodes("a", "b"), depth=3)
    admission.admit(nodes("b", "c"), depth=2)  # b is lowered; c is new
    admission.admit(nodes("b", "c"), depth=3)  # deeper again: nothing changes
    assert (frontier.queued, frontier.outstanding) == (3, 3)
    # b goes to the back of the depth-2 queue; its old entry at depth 3 is skipped.
    assert [(i.key, i.depth) for i in pop_all(frontier)] == [("b", 2), ("c", 2), ("a", 3)]
    assert frontier.queued == 0


def test_an_item_in_flight_or_waiting_to_be_tried_again_keeps_its_depth() -> None:
    frontier = Frontier(max_depth=10)
    admission = Admission(frontier, SCOPE)
    admission.admit(nodes("a", "b"), depth=2)
    a = frontier.pop(NOW)
    b = frontier.pop(NOW)
    assert a is not None
    assert b is not None
    frontier.schedule_retry(b, due=NOW)
    admission.admit(nodes("a", "b"), depth=1)  # neither is queued: neither moves
    assert frontier.pop(NOW) is b
    assert b.depth == 2


def test_under_max_depth_an_item_waits_until_nothing_two_levels_shallower_is_unfinished() -> None:
    # The two-level window (DESIGN.md §5.1): with the depth-0 item unfinished, depth 1 may
    # be dispatched but not depth 2, whatever is still to finish at depth 1.
    frontier = Frontier(max_depth=10)
    admission = Admission(frontier, SCOPE)
    admission.admit(nodes("root"), depth=0)
    root = frontier.pop(NOW)
    assert root is not None
    admission.admit(nodes("x"), depth=1)
    admission.admit(nodes("y"), depth=2)  # as if from a page found by another path
    x = frontier.pop(NOW)
    assert x is not None
    assert frontier.pop(NOW) is None  # y is outside the window
    frontier.finish(root)
    y = frontier.pop(NOW)  # depth 1 is the shallowest unfinished, with x in flight
    assert y is not None
    admission.admit(nodes("z"), depth=3)
    assert frontier.pop(NOW) is None  # x, at depth 1, isn't finished
    frontier.finish(x)
    assert frontier.pop(NOW) == CrawlItem("z", "z", 3)


@pytest.mark.parametrize("unfinished", ["in flight", "retrying", "held"])
def test_items_not_yet_finished_hold_the_window(unfinished: str) -> None:
    # An item waiting to be tried again, or held by the block guard until its run of 403s
    # is settled, may still turn out to be a page whose links are shallower paths.
    frontier = Frontier(max_depth=10)
    admission = Admission(frontier, SCOPE)
    admission.admit(nodes("a"), depth=1)
    a = frontier.pop(NOW)
    assert a is not None
    admission.admit(nodes("deep"), depth=3)
    if unfinished == "retrying":
        frontier.schedule_retry(a, due=NOW + 10)
    assert frontier.pop(NOW) is None
    assert frontier.outstanding == 2
    if unfinished == "retrying":
        assert frontier.pop(NOW + 10) is a
    frontier.finish(a)
    assert frontier.pop(NOW + 10) == CrawlItem("deep", "deep", 3)


def test_links_beyond_max_depth_are_counted_and_not_claimed() -> None:
    frontier = Frontier(max_depth=1)
    admission = Admission(frontier, SCOPE)
    assert admission.admit(nodes("a", "b"), depth=1) == 2
    assert admission.admit(nodes("a", "c", "d", "c", "e", "f"), depth=2) == 0
    assert admission.seen == {"a", "b"}
    assert admission.beyond == {"c": "c", "d": "d", "e": "e", "f": "f"}
    assert admission.unique_urls == 6
    # A shorter path admits one; the summary counts the rest, with the first few.
    assert admission.admit(nodes("d"), depth=1) == 1
    assert admission.unique_urls == 6
    assert admission.rejections() == ({DEPTH_LIMIT: 3}, {DEPTH_LIMIT: ["c", "e", "f"]})
    assert not admission.rejected  # the rules' own counts are unchanged


def test_a_redirect_claims_a_url_found_beyond_max_depth() -> None:
    frontier = Frontier(max_depth=0)
    admission = Admission(frontier, SCOPE)
    url = "https://example.com/r"
    admission.admit([(url, url)], depth=1)
    assert admission.admit_hop(set(), url, url) is Hop.FOLLOW
    assert admission.beyond == {}
    assert admission.rejections() == ({}, {})


def test_beyond_max_depth_the_other_rules_dont_count_a_url() -> None:
    # It isn't claimed, so it would be counted again each time it's found.
    frontier = Frontier(max_depth=0)
    admission = Admission(frontier, SCOPE, max_query_variants=1)
    calendar = [f"https://example.com/cal?month={n}" for n in range(3)]
    for _ in range(2):
        admission.admit(links(*calendar), depth=1)
    assert not admission.rejected
    assert admission.query_variants == {}
    assert admission.rejections()[0] == {DEPTH_LIMIT: 3}


def test_max_depth_0_queues_the_start_url_alone() -> None:
    frontier = Frontier(max_depth=0)
    admission = Admission(frontier, SCOPE)
    assert admission.admit(nodes("start"), depth=0) == 1
    start = frontier.pop(NOW)
    assert start is not None
    assert admission.admit(nodes("a", "b"), depth=1) == 0
    frontier.finish(start)
    assert frontier.outstanding == 0


def test_the_start_url_admitted_again_after_its_first_try_is_dispatched() -> None:
    # The http:// fallback admits the start URL again, once the https:// try has finished.
    frontier = Frontier(max_depth=2)
    admission = Admission(frontier, SCOPE)
    admission.admit(nodes("https"), depth=0)
    first = frontier.pop(NOW)
    assert first is not None
    frontier.finish(first)
    admission.seen.clear()
    admission.admit(nodes("http"), depth=0)
    start = frontier.pop(NOW)
    assert start == CrawlItem("http", "http", 0)
    admission.admit(nodes("a"), depth=1)
    admission.admit(nodes("deep"), depth=2)
    a = frontier.pop(NOW)
    assert a is not None
    assert frontier.pop(NOW) is None  # the window counts from depth 0 again
    frontier.finish(start)
    assert frontier.pop(NOW) == CrawlItem("deep", "deep", 2)


class _Crawl:
    """A site's crawl, driven through the frontier as the dispatcher drives it, with
    hypothesis choosing what happens to each fetch, and when. A forbidden page's fetch ends
    without links; any other page's fetch may fail and be tried again, or be held by the
    block guard, as many as 3 times, before it's a page."""

    TRIES = 3

    def __init__(self, site: list[list[int]], forbidden: frozenset[int], max_depth: int) -> None:
        self.site = site
        self.forbidden = forbidden
        self.frontier = Frontier(max_depth)
        self.admission = Admission(self.frontier, SCOPE)
        self.admission.admit(nodes(0), depth=0)
        self.now = 0.0
        self.in_flight: list[CrawlItem] = []
        self.held: list[CrawlItem] = []  # by the block guard, until their run is settled
        self.dispatched: dict[str, int] = {}  # each key's depth when it was first dispatched
        self.tries: Counter[str] = Counter()
        self.again: Counter[str] = Counter()  # tries to be followed by another

    def dispatch(self, concurrency: int) -> None:
        while len(self.in_flight) < concurrency and (item := self.frontier.pop(self.now)):
            assert self.dispatched.setdefault(item.key, item.depth) == item.depth
            self.tries[item.key] += 1
            self.in_flight.append(item)

    def step(self, data: st.DataObject) -> None:
        events = [
            event
            for event, possible in [
                (self.complete, self.in_flight),
                (self.settle, self.held),
                (self.wait, self.frontier.retrying),
            ]
            if possible
        ]
        assert events, "items are outstanding, but none can be dispatched"
        data.draw(st.sampled_from(events))(data)
        frontier = self.frontier
        assert frontier.outstanding == (
            frontier.queued + len(self.in_flight) + len(self.held) + frontier.retrying
        )

    def complete(self, data: st.DataObject) -> None:
        item = self.in_flight.pop(data.draw(st.integers(0, len(self.in_flight) - 1)))
        node = int(item.key)
        outcome = data.draw(st.sampled_from(["page", "retry", "held"]))
        if outcome == "held" and self.tries[item.key] < self.TRIES:
            self.held.append(item)
        elif node in self.forbidden:
            self.frontier.finish(item)  # its 403 is final: no links
        elif outcome == "retry" and self.tries[item.key] < self.TRIES:
            due = self.now + data.draw(st.sampled_from([0.0, 1.0]))
            self.frontier.schedule_retry(item, due)
            self.again[item.key] += 1
        else:
            self.admission.admit(nodes(*self.site[node]), item.depth + 1)
            self.frontier.finish(item)

    def settle(self, data: st.DataObject) -> None:
        """The block guard settles its run: a page really forbidden is final; any other was
        refused by a block that's over, and is tried again."""
        for item in self.held:
            if int(item.key) in self.forbidden:
                self.frontier.finish(item)
            else:
                self.frontier.schedule_retry(item, self.now)
                self.again[item.key] += 1
        self.held.clear()

    def wait(self, data: st.DataObject) -> None:
        due = self.frontier.next_retry_at()
        assert due is not None
        self.now = max(self.now, due)


@settings(max_examples=300)
@given(st.data())
def test_max_depth_dispatches_exactly_the_pages_within_reach(data: st.DataObject) -> None:
    # A.10's property on the crawler's own frontier: whatever order fetches complete in,
    # however many fail and wait to be tried again, and whichever the block guard holds
    # until their run of 403s is settled, the pages dispatched are exactly those within
    # max_depth links of the start, each at its breadth-first depth. A forbidden page is
    # dispatched, but its links are never found.
    size = data.draw(st.integers(1, 60), label="pages")
    site = make_site(
        data.draw(st.integers(0, 1 << 16), label="seed"),
        size,
        branching=data.draw(st.sampled_from([1.5, 3.0])),
        cross_links=data.draw(st.integers(0, 2)),
    )
    forbidden = data.draw(st.frozensets(st.integers(1, size), max_size=size // 4))
    truth = bfs([[] if node in forbidden else links for node, links in enumerate(site)])
    max_depth = data.draw(st.integers(0, max(truth.values()) + 1), label="max depth")
    concurrency = data.draw(st.integers(1, 6), label="concurrency")
    crawl = _Crawl(site, forbidden, max_depth)
    while crawl.frontier.outstanding:
        crawl.dispatch(concurrency)
        crawl.step(data)
    assert crawl.dispatched == {
        str(node): depth for node, depth in truth.items() if depth <= max_depth
    }
    assert all(crawl.tries[key] == 1 + crawl.again[key] for key in crawl.tries)
    assert crawl.frontier.pop(math.inf) is None


@pytest.mark.parametrize(
    ("branching", "cross_links"), [(3.0, 1), (4.0, 3)], ids=["sparse", "denser"]
)
def test_max_depth_is_exact_on_simulated_sites(branching: float, cross_links: int) -> None:
    # A.10's simulation on the crawler's own frontier: random sites of 1,000 pages, 32
    # fetches in flight with heavy-tailed latencies, and the limit through the middle.
    for seed in range(10):
        run = simulate_depth(seed, 1000, branching, cross_links)
        assert run.fetched == run.truth


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


def links(*urls: str) -> list[tuple[str, str]]:
    return [(url, url) for url in urls]


def test_admit_rejects_what_a_trap_rule_or_the_extension_filter_does() -> None:
    frontier = Frontier()
    admission = Admission(frontier, SCOPE, disallowing("/private"))
    long = "https://example.com/" + "x" * 2100
    admitted = admission.admit(
        links(
            long,
            "https://example.com/a/a/a/a",
            "https://example.com/report.pdf",
            "https://example.com/style.css",
            "https://example.com/private/page",
            "https://example.com/page",
        ),
        depth=1,
    )
    assert admitted == 1
    assert admission.rejected == {
        LONG_URL: 1,
        REPEATED_SEGMENT: 1,
        SKIPPED_EXTENSION: 2,
        "robots.txt": 1,
    }
    assert admission.examples[SKIPPED_EXTENSION] == [
        "https://example.com/report.pdf",
        "https://example.com/style.css",
    ]
    # Claimed: found again, they're neither checked nor counted again.
    assert admission.admit(links(long, "https://example.com/report.pdf"), depth=2) == 0
    assert admission.rejected[SKIPPED_EXTENSION] == 2
    assert [item.url for item in pop_all(frontier)] == ["https://example.com/page"]


def test_the_extension_filter_can_be_turned_off() -> None:
    frontier = Frontier()
    admission = Admission(frontier, SCOPE, skip_extensions=False)
    assert admission.admit(links("https://example.com/old.pdf"), depth=1) == 1
    assert not admission.rejected


def test_a_path_may_have_a_limited_number_of_query_strings() -> None:
    frontier = Frontier()
    admission = Admission(frontier, SCOPE, max_query_variants=3)
    calendar = [f"https://example.com/cal?month={n}" for n in range(5)]
    assert admission.admit(links(*calendar), depth=1) == 3
    assert admission.rejected == {QUERY_VARIANTS: 2}
    assert admission.examples[QUERY_VARIANTS] == calendar[3:]
    # Other paths, and the path without a query, have their own count.
    others = ["https://example.com/cal", "https://example.com/list?page=1"]
    assert admission.admit(links(*others), depth=1) == 2
    # Another scheme is another path, for this count.
    assert admission.admit(links("http://example.com/cal?month=9"), depth=1) == 1


def test_query_variants_count_only_new_urls() -> None:
    frontier = Frontier()
    admission = Admission(frontier, SCOPE, max_query_variants=2)
    for _ in range(5):
        admission.admit(links("https://example.com/cal?m=1", "https://example.com/cal?m=2"), 1)
    assert not admission.rejected


@pytest.mark.parametrize(
    ("url", "hop", "rule"),
    [
        ("https://example.com/" + "x" * 2100, Hop.TRAP, LONG_URL),
        ("https://example.com/a/a/a/a", Hop.TRAP, REPEATED_SEGMENT),
        ("https://example.com/private/x", Hop.DISALLOWED, "robots.txt"),
        # A redirect's target is followed whatever its extension: its response says what
        # it is.
        ("https://example.com/report.pdf", Hop.FOLLOW, None),
    ],
)
def test_admit_hop_applies_the_trap_rules_and_robots_txt(
    url: str, hop: Hop, rule: str | None
) -> None:
    admission = Admission(Frontier(), SCOPE, disallowing("/private"))
    assert admission.admit_hop(set(), url, url) is hop
    assert admission.rejected == ({rule: 1} if rule else {})
    assert url in admission.seen


def test_admit_hop_counts_query_variants() -> None:
    admission = Admission(Frontier(), SCOPE, max_query_variants=1)
    url = "https://example.com/cal?month=2"
    assert admission.admit_hop(set(), "https://example.com/cal?month=1", "1") is Hop.FOLLOW
    assert admission.admit_hop(set(), url, url) is Hop.TRAP
    assert admission.rejected == {QUERY_VARIANTS: 1}
