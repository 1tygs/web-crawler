"""What gets crawled, and in what order (DESIGN.md §4.2, §5.1).

Admission decides whether a newly found URL is crawled, and claims it if so; Frontier holds
the claimed URLs until they're dispatched, shallowest first, and those waiting to be tried
again, and counts the ones not yet finished. Both are synchronous and do no I/O: the engine
feeds them events and the time.
"""

import dataclasses
import enum
import heapq
import itertools
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterable

from web_crawler.robots import RobotsPolicy
from web_crawler.types import CrawlItem
from web_crawler.urls import (
    MAX_QUERY_VARIANTS,
    MAX_SEGMENT_REPEATS,
    MAX_URL_LENGTH,
    QUERY_VARIANTS,
    SKIPPED_EXTENSION,
    Scope,
    has_skipped_extension,
    trap_rule,
    without_query,
)

ROBOTS = "robots.txt"  # the rule that rejects what robots.txt disallows
DEPTH_LIMIT = "depth limit"  # URLs found only beyond --max-depth
EXAMPLES = 3  # rejected URLs kept per rule, to show in the summary

# Told of each URL a rule keeps from being crawled: the rule, the URL, and whether it's one of
# the rule's examples, the first few, which the summary shows.
type OnRejected = Callable[[str, str, bool], object]


class Frontier:
    """Admitted items, breadth-first: one FIFO queue per depth, shallowest dispatched first.

    An item that failed in a way worth another try waits out its backoff in a heap ordered
    by when it's due, holding no permit and no pipeline slot (DESIGN.md §5.4). Once due, it
    goes to the front of its depth's queue: it has waited its turn already.

    outstanding counts the items admitted and not yet finished: queued, in flight, waiting
    to be tried again, or held by the block guard. A page's links are admitted before the
    page finishes, so outstanding is 0 exactly when the crawl is complete.

    Under --max-depth, the crawl reaches exactly the pages within max_depth links of the
    start, however its fetches interleave (DESIGN.md §5.1), by two rules:
    - an item found again at a shallower depth while it's queued is lowered to that depth;
    - the two-level window: an item at depth d is dispatched only once nothing at depth
      d - 2 or shallower is unfinished.
    A page at depth d can become shallower only through a page at depth d - 2 or less, so
    once those have all finished, its depth is final: it's dispatched at the right depth,
    and its links are admitted at the right depth too.
    """

    def __init__(self, max_depth: int | None = None) -> None:
        self.max_depth = max_depth
        self._queues: list[deque[CrawlItem]] = []
        self._shallowest = 0  # no queue shallower than this has items
        self._retries: list[tuple[float, int, CrawlItem]] = []  # (due, order of arrival, item)
        self._arrivals = itertools.count()  # so that items due at once leave in order
        self._unfinished: list[int] = []  # by depth: items admitted and not yet finished
        self._low = 0  # no depth shallower than this has unfinished items
        # Under --max-depth, each queued item by its key, so that one found again at a
        # shallower depth can be lowered. A lowered item is queued again at its new depth,
        # and the old entry is skipped when it comes up: it's no longer the key's.
        self.lowerable: dict[str, CrawlItem] | None = None if max_depth is None else {}
        self.queued = 0  # items waiting in a queue
        self.outstanding = 0

    def push(self, item: CrawlItem) -> None:
        depth = item.depth
        while len(self._queues) <= depth:
            self._queues.append(deque())
            self._unfinished.append(0)
        self._queues[depth].append(item)
        self._unfinished[depth] += 1
        self._shallowest = min(self._shallowest, depth)
        # A page's links are a depth deeper than the page, so the shallowest unfinished
        # depth only moves on, but for the start URL, admitted again for its http://
        # fallback once the https:// try has finished.
        self._low = min(self._low, depth)
        if self.lowerable is not None:
            self.lowerable[item.key] = item
        self.queued += 1
        self.outstanding += 1

    def pop(self, now: float) -> CrawlItem | None:
        """The next item to fetch at time now, or None if none is queued or due, or, under
        --max-depth, if none is inside the window."""
        if self._retries and self._retries[0][0] <= now:
            self._release(now)
        queues = self._queues
        lowerable = self.lowerable
        while self._shallowest < len(queues):
            queue = queues[self._shallowest]
            while queue:
                if lowerable is not None:
                    item = queue[0]
                    if lowerable.get(item.key) is not item:  # lowered since: queued again
                        queue.popleft()
                        continue
                    if self._shallowest > self._low + 1 and self._outside_window():
                        return None
                    del lowerable[item.key]
                self.queued -= 1
                return queue.popleft()
            self._shallowest += 1
        return None

    def _outside_window(self) -> bool:
        """Whether the shallowest queue is more than a depth deeper than the shallowest
        unfinished one. The queue has an item, so the search ends there at the latest."""
        unfinished = self._unfinished
        while not unfinished[self._low]:
            self._low += 1
        return self._shallowest > self._low + 1

    def _release(self, now: float) -> None:
        """Move the retries due by now to the front of their queues, the earliest first."""
        due = []
        while self._retries and self._retries[0][0] <= now:
            due.append(heapq.heappop(self._retries)[2])
        for item in reversed(due):  # each was popped, so its depth has a queue
            self._queues[item.depth].appendleft(item)
            self._shallowest = min(self._shallowest, item.depth)
            if self.lowerable is not None:
                self.lowerable[item.key] = item
        self.queued += len(due)

    def lower(self, item: CrawlItem, depth: int) -> None:
        """item, which is queued, was found again at depth, shallower than its own: queue it
        at depth instead (DESIGN.md §5.1)."""
        assert self.lowerable is not None, "items are lowered under --max-depth only"
        lowered = dataclasses.replace(item, depth=depth)
        self.lowerable[item.key] = lowered
        self._queues[depth].append(lowered)
        self._unfinished[item.depth] -= 1
        self._unfinished[depth] += 1
        self._shallowest = min(self._shallowest, depth)

    def schedule_retry(self, item: CrawlItem, due: float) -> None:
        """An item that was popped is to be fetched again, no sooner than due. It stays
        outstanding, and unfinished at its depth, until then."""
        heapq.heappush(self._retries, (due, next(self._arrivals), item))

    def next_retry_at(self) -> float | None:
        """When the next retry is due, or None if there are none."""
        return self._retries[0][0] if self._retries else None

    @property
    def retrying(self) -> int:
        """Items waiting to be tried again."""
        return len(self._retries)

    def finish(self, item: CrawlItem) -> None:
        """item, which was popped, is done with, whatever its outcome."""
        self.outstanding -= 1
        self._unfinished[item.depth] -= 1


class Hop(enum.Enum):
    """What admission says about a redirect's target (DESIGN.md §5.6)."""

    FOLLOW = "follow"  # the item requests it: it claimed it now, or had already
    CLAIMED = "claimed"  # another item claimed it, and prints it
    OUT_OF_SCOPE = "out of scope"
    DISALLOWED = "disallowed"  # by robots.txt
    TRAP = "trap"  # a trap rule rejects it


class Admission:
    """Claims each URL once, on discovery: from then on, no other page can queue it again.

    seen holds dedupe keys. A key is checked and claimed with no await in between, so on one
    event loop a claim is atomic without a lock (DESIGN.md §4.3). Every redirect target is
    claimed the same way, so a page reached by redirect is fetched once too.

    The rules, cheapest first (DESIGN.md §5.1): --max-depth; the trap rules, which bound a
    crawl that a site's links could make endless (a URL's length, a segment repeated in its
    path, the query strings one path has); the extension filter, which skips files that are
    almost never HTML, unless skip_extensions is off; and robots.txt. A URL that a rule
    rejects is claimed as well, so it's checked once however often it's found, and counted
    by rule with a few examples: nothing is skipped silently.

    The depth limit is the exception. A URL found beyond it isn't claimed, since a shorter
    path found later can still admit it, and the other rules don't see it until then: they
    would count it again each time it's found. beyond keeps it until then, for the summary.

    on_rejected hears of each URL rejected, as it is, for the -v log.
    """

    def __init__(  # noqa: PLR0913  (the rules' settings, which have defaults)
        self,
        frontier: Frontier,
        scope: Scope,
        robots: RobotsPolicy | None = None,
        *,
        max_url_length: int = MAX_URL_LENGTH,
        max_segment_repeats: int = MAX_SEGMENT_REPEATS,
        max_query_variants: int = MAX_QUERY_VARIANTS,
        skip_extensions: bool = True,
        on_rejected: OnRejected | None = None,
    ) -> None:
        self.seen: set[str] = set()
        self.scope = scope  # links arrive checked; redirect targets are checked here
        self.robots = robots or RobotsPolicy("", {})  # with no files, nothing is disallowed
        self.rejected: Counter[str] = Counter()  # by rule
        self.examples: dict[str, list[str]] = {}  # the first few URLs each rule rejected
        self.query_variants: dict[str, int] = {}  # URLs with a query, by path
        self.beyond: dict[str, str] = {}  # URLs found only beyond --max-depth, by key
        self._frontier = frontier
        self._max_url_length = max_url_length
        self._max_segment_repeats = max_segment_repeats
        self._max_query_variants = max_query_variants
        self._skip_extensions = skip_extensions
        self._on_rejected: OnRejected = on_rejected or _ignore

    def admit(self, links: Iterable[tuple[str, str]], depth: int) -> int:
        """Claim each (key, url) not seen before and, unless a rule rejects it, queue it at
        depth. Returns how many were queued."""
        seen = self.seen
        frontier = self._frontier
        if frontier.max_depth is not None and depth > frontier.max_depth:
            beyond = self.beyond
            for key, url in links:
                if key not in seen and key not in beyond:
                    beyond[key] = url
                    self._on_rejected(DEPTH_LIMIT, url, len(beyond) <= EXAMPLES)
            return 0
        lowerable = frontier.lowerable  # None, unless under --max-depth
        push = frontier.push
        queued = 0
        for key, url in links:
            if key in seen:
                if (
                    lowerable is not None
                    and (item := lowerable.get(key)) is not None
                    and item.depth > depth
                ):
                    frontier.lower(item, depth)  # a shorter path to an item still queued
                continue
            seen.add(key)
            if lowerable is not None:
                self.beyond.pop(key, None)  # it may have been found beyond the limit before
            if (rule := self._rule(url, links=True)) is not None:
                self._reject(rule, url)
                continue
            push(CrawlItem(url=url, key=key, depth=depth))
            queued += 1
        return queued

    def admit_hop(self, chain: Collection[str], url: str, key: str) -> Hop:
        """Whether an item may request url, a redirect target with dedupe key key. chain
        holds the keys of the URLs the item has requested so far.

        A target already in the chain is followed again: a cookie-gated page redirects to a
        page that sets the cookie, which redirects back. A real loop ends at the fetcher's
        hop limit.
        """
        if not self.scope.contains(url):
            return Hop.OUT_OF_SCOPE
        if key in chain:
            return Hop.FOLLOW
        if key in self.seen:
            return Hop.CLAIMED
        self.seen.add(key)
        if self.beyond:
            self.beyond.pop(key, None)
        if (rule := self._rule(url, links=False)) is None:
            return Hop.FOLLOW
        self._reject(rule, url)
        return Hop.DISALLOWED if rule == ROBOTS else Hop.TRAP

    def _rule(self, url: str, *, links: bool) -> str | None:
        """The rule that keeps url, newly claimed, from being crawled, if any. The extension
        filter applies to links only: a redirect's target is followed, and its response says
        what it is."""
        if (rule := trap_rule(url, self._max_url_length, self._max_segment_repeats)) is not None:
            return rule
        if "?" in url:
            path = without_query(url)
            variants = self.query_variants[path] = self.query_variants.get(path, 0) + 1
            if variants > self._max_query_variants:
                return QUERY_VARIANTS
        if links and self._skip_extensions and has_skipped_extension(url):
            return SKIPPED_EXTENSION
        return None if self.robots.allowed(url) else ROBOTS

    def _reject(self, rule: str, url: str) -> None:
        self.rejected[rule] += 1
        examples = self.examples.setdefault(rule, [])
        if sampled := len(examples) < EXAMPLES:
            examples.append(url)
        self._on_rejected(rule, url, sampled)

    @property
    def unique_urls(self) -> int:
        """The in-scope URLs found: those claimed, and those found only beyond --max-depth."""
        return len(self.seen) + len(self.beyond)

    def rejections(self) -> tuple[Counter[str], dict[str, list[str]]]:
        """The URLs found that weren't crawled, counted by rule, and the first few of each:
        those a rule rejected, and those found only beyond --max-depth."""
        rejected, examples = Counter(self.rejected), dict(self.examples)
        if self.beyond:
            rejected[DEPTH_LIMIT] = len(self.beyond)
            examples[DEPTH_LIMIT] = list(itertools.islice(self.beyond.values(), EXAMPLES))
        return rejected, examples


def _ignore(rule: str, url: str, sampled: bool) -> None:
    pass
