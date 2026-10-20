"""What gets crawled, and in what order (DESIGN.md §4.2, §5.1).

Admission decides whether a newly found URL is crawled, and claims it if so; Frontier holds
the claimed URLs until they're dispatched, shallowest first, and counts the ones not yet
finished. Both are synchronous and do no I/O: the engine feeds them events.
"""

import enum
from collections import deque
from collections.abc import Collection, Iterable

from web_crawler.types import CrawlItem
from web_crawler.urls import Scope


class Frontier:
    """Admitted items, breadth-first: one FIFO queue per depth, shallowest dispatched first.

    outstanding counts the items admitted and not yet finished. A page's links are admitted
    before the page finishes, so outstanding is 0 exactly when the crawl is complete.
    """

    def __init__(self) -> None:
        self._queues: list[deque[CrawlItem]] = []
        self._shallowest = 0  # no queue shallower than this has items
        self.outstanding = 0

    def push(self, item: CrawlItem) -> None:
        while len(self._queues) <= item.depth:
            self._queues.append(deque())
        self._queues[item.depth].append(item)
        self._shallowest = min(self._shallowest, item.depth)
        self.outstanding += 1

    def pop(self) -> CrawlItem | None:
        """The next item to fetch, or None if none is queued."""
        queues = self._queues
        while self._shallowest < len(queues):
            if queue := queues[self._shallowest]:
                return queue.popleft()
            self._shallowest += 1
        return None

    def finish(self) -> None:
        """An item that was popped is done with, whatever its outcome."""
        self.outstanding -= 1


class Hop(enum.Enum):
    """What admission says about a redirect's target (DESIGN.md §5.6)."""

    FOLLOW = "follow"  # the item requests it: it claimed it now, or had already
    CLAIMED = "claimed"  # another item claimed it, and prints it
    OUT_OF_SCOPE = "out of scope"


class Admission:
    """Claims each URL once, on discovery: from then on, no other page can queue it again.

    seen holds dedupe keys. A key is checked and claimed with no await in between, so on one
    event loop a claim is atomic without a lock (DESIGN.md §4.3). Every redirect target is
    claimed the same way, so a page reached by redirect is fetched once too.
    """

    def __init__(self, frontier: Frontier, scope: Scope) -> None:
        self.seen: set[str] = set()
        self.scope = scope  # links arrive checked; redirect targets are checked here
        self._frontier = frontier

    def admit(self, links: Iterable[tuple[str, str]], depth: int) -> int:
        """Claim and queue each (key, url) not seen before, at depth. Returns how many."""
        seen = self.seen
        push = self._frontier.push
        before = len(seen)
        for key, url in links:
            if key not in seen:
                seen.add(key)
                push(CrawlItem(url=url, key=key, depth=depth))
        return len(seen) - before

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
        return Hop.FOLLOW
