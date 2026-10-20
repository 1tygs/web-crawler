"""What gets crawled, and in what order (DESIGN.md §4.2, §5.1).

Admission decides whether a newly found URL is crawled, and claims it if so; Frontier holds
the claimed URLs until they're dispatched, shallowest first, and counts the ones not yet
finished. Both are synchronous and do no I/O: the engine feeds them events.
"""

from collections import deque
from collections.abc import Iterable

from web_crawler.types import CrawlItem


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


class Admission:
    """Claims each URL once, on discovery: from then on, no other page can queue it again.

    seen holds dedupe keys. A key is checked and claimed with no await in between, so on one
    event loop a claim is atomic without a lock (DESIGN.md §4.3).
    """

    def __init__(self, frontier: Frontier) -> None:
        self.seen: set[str] = set()
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
