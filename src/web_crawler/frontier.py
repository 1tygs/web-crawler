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
        while len(self._queues) <= item.depth:
            self._queues.append(deque())
        self._queues[item.depth].append(item)
        self._shallowest = min(self._shallowest, item.depth)
        self.outstanding += 1

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
        for item in reversed(due):  # each was popped, so its depth has a queue
            self._queues[item.depth].appendleft(item)
            self._shallowest = min(self._shallowest, item.depth)

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
    TRAP = "trap"  # a trap rule rejects it


class Admission:
    """Claims each URL once, on discovery: from then on, no other page can queue it again.

    seen holds dedupe keys. A key is checked and claimed with no await in between, so on one
    event loop a claim is atomic without a lock (DESIGN.md §4.3). Every redirect target is
    claimed the same way, so a page reached by redirect is fetched once too.

    The rules, cheapest first (DESIGN.md §5.1): the trap rules, which bound a crawl that
    a site's links could make endless (a URL's length, a segment repeated in its path, the
    query strings one path has); the extension filter, which skips files that are almost
    never HTML, unless skip_extensions is off; and robots.txt. A URL that a rule rejects is
    claimed as well, so it's checked once however often it's found, and counted by rule
    with a few examples: nothing is skipped silently.
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
    ) -> None:
        self.seen: set[str] = set()
        self.scope = scope  # links arrive checked; redirect targets are checked here
        self.robots = robots or RobotsPolicy("", {})  # with no files, nothing is disallowed
        self.rejected: Counter[str] = Counter()  # by rule
        self.examples: dict[str, list[str]] = {}  # the first few URLs each rule rejected
        self.query_variants: dict[str, int] = {}  # URLs with a query, by path
        self._frontier = frontier
        self._max_url_length = max_url_length
        self._max_segment_repeats = max_segment_repeats
        self._max_query_variants = max_query_variants
        self._skip_extensions = skip_extensions

    def admit(self, links: Iterable[tuple[str, str]], depth: int) -> int:
        """Claim each (key, url) not seen before and, unless a rule rejects it, queue it at
        depth. Returns how many were queued."""
        seen = self.seen
        push = self._frontier.push
        queued = 0
        for key, url in links:
            if key in seen:
                continue
            seen.add(key)
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
        if len(examples) < EXAMPLES:
            examples.append(url)
