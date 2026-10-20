"""What gets crawled, and in what order (DESIGN.md §4.2, §5.1).

Admission decides whether a newly found URL is crawled, and claims it if so; Frontier holds
the claimed URLs until they're dispatched, shallowest first, and those waiting to be tried
again, and counts the ones not yet finished. Both are synchronous and do no I/O: the engine
feeds them events and the time.
"""

import enum
import heapq
import itertools
from collections import Counter, deque
from collections.abc import Collection, Iterable

from web_crawler.robots import RobotsPolicy
from web_crawler.types import CrawlItem
from web_crawler.urls import Scope

ROBOTS = "robots.txt"  # the rule that rejects what robots.txt disallows
EXAMPLES = 3  # rejected URLs kept per rule, to show in the summary


class Frontier:
    """Admitted items, breadth-first: one FIFO queue per depth, shallowest dispatched first.

    An item that failed in a way worth another try waits out its backoff in a heap ordered
    by when it's due, holding no permit and no pipeline slot (DESIGN.md §5.4). Once due, it
    goes to the front of its depth's queue: it has waited its turn already.

    outstanding counts the items admitted and not yet finished, those waiting to be tried
    again among them. A page's links are admitted before the page finishes, so outstanding
    is 0 exactly when the crawl is complete.
    """

    def __init__(self) -> None:
        self._queues: list[deque[CrawlItem]] = []
        self._shallowest = 0  # no queue shallower than this has items
        self._retries: list[tuple[float, int, CrawlItem]] = []  # (due, order of arrival, item)
        self._arrivals = itertools.count()  # so that items due at once leave in order
        self.outstanding = 0

    def push(self, item: CrawlItem) -> None:
        self._queue_at(item.depth).append(item)
        self.outstanding += 1

    def _queue_at(self, depth: int) -> deque[CrawlItem]:
        while len(self._queues) <= depth:
            self._queues.append(deque())
        self._shallowest = min(self._shallowest, depth)
        return self._queues[depth]

    def pop(self, now: float) -> CrawlItem | None:
        """The next item to fetch at time now, or None if none is queued or due."""
        if self._retries and self._retries[0][0] <= now:
            self._release(now)
        queues = self._queues
        while self._shallowest < len(queues):
            if queue := queues[self._shallowest]:
                return queue.popleft()
            self._shallowest += 1
        return None

    def _release(self, now: float) -> None:
        """Move the retries due by now to the front of their queues, the earliest first."""
        due = []
        while self._retries and self._retries[0][0] <= now:
            due.append(heapq.heappop(self._retries)[2])
        for item in reversed(due):
            self._queue_at(item.depth).appendleft(item)

    def schedule_retry(self, item: CrawlItem, due: float) -> None:
        """An item that was popped is to be fetched again, no sooner than due. It stays
        outstanding until then."""
        heapq.heappush(self._retries, (due, next(self._arrivals), item))

    def next_retry_at(self) -> float | None:
        """When the next retry is due, or None if there are none."""
        return self._retries[0][0] if self._retries else None

    @property
    def retrying(self) -> int:
        """Items waiting to be tried again."""
        return len(self._retries)

    def finish(self) -> None:
        """An item that was popped is done with, whatever its outcome."""
        self.outstanding -= 1


class Hop(enum.Enum):
    """What admission says about a redirect's target (DESIGN.md §5.6)."""

    FOLLOW = "follow"  # the item requests it: it claimed it now, or had already
    CLAIMED = "claimed"  # another item claimed it, and prints it
    OUT_OF_SCOPE = "out of scope"
    DISALLOWED = "disallowed"  # by robots.txt


class Admission:
    """Claims each URL once, on discovery: from then on, no other page can queue it again.

    seen holds dedupe keys. A key is checked and claimed with no await in between, so on one
    event loop a claim is atomic without a lock (DESIGN.md §4.3). Every redirect target is
    claimed the same way, so a page reached by redirect is fetched once too.

    A URL that a rule rejects is claimed as well, so it's checked once however often it's
    found, and counted by rule with a few examples: nothing is skipped silently.
    """

    def __init__(
        self, frontier: Frontier, scope: Scope, robots: RobotsPolicy | None = None
    ) -> None:
        self.seen: set[str] = set()
        self.scope = scope  # links arrive checked; redirect targets are checked here
        self.robots = robots or RobotsPolicy("", {})  # with no files, nothing is disallowed
        self.rejected: Counter[str] = Counter()  # by rule
        self.examples: dict[str, list[str]] = {}  # the first few URLs each rule rejected
        self._frontier = frontier

    def admit(self, links: Iterable[tuple[str, str]], depth: int) -> int:
        """Claim each (key, url) not seen before and, unless a rule rejects it, queue it at
        depth. Returns how many were queued."""
        seen = self.seen
        allowed = self.robots.allowed
        push = self._frontier.push
        queued = 0
        for key, url in links:
            if key in seen:
                continue
            seen.add(key)
            if not allowed(url):
                self._reject(ROBOTS, url)
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
        if not self.robots.allowed(url):
            self._reject(ROBOTS, url)
            return Hop.DISALLOWED
        return Hop.FOLLOW

    def _reject(self, rule: str, url: str) -> None:
        self.rejected[rule] += 1
        examples = self.examples.setdefault(rule, [])
        if len(examples) < EXAMPLES:
            examples.append(url)
