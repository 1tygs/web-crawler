"""Appendix A.10: do the --max-depth rules crawl exactly the pages within N links of the start?

Usage: python bench/depth_sim.py [SEEDS=300]

Event-driven simulation of a concurrent, shallowest-first crawl over random site graphs with
heavy-tailed per-page latency, compared against breadth-first-search ground truth. Rules:

  naive   depth fixed at first discovery (what Scrapy's DEPTH_LIMIT does)
  draft   the first draft of DESIGN.md §5.1, read generously: queued and in-flight items are
          lowered when found shallower; pages whose children were cut off keep those links and
          re-admit them when the page is found shallower, recursively through other such pages
  window  DESIGN.md §5.1 now: no stored links; an item at depth d is dispatched only when nothing at
          depth <= d-2 is still queued or in flight; queued items are lowered as before
  crawler the window as the crawler runs it: its own Frontier and Admission (web_crawler.frontier)
"""
import collections
import heapq
import math
import random
import statistics
import sys

from web_crawler.frontier import Admission, Frontier
from web_crawler.urls import Scope


def make_site(rng, n, branching, cross):
    """A tree grown by a branching process (sections -> pages), plus `cross` random links per page
    (related posts, sidebars, tag pages) that create shortcuts of different lengths."""
    adj = [[] for _ in range(n)]
    frontier, nxt = [0], 1
    while nxt < n and frontier:
        new = []
        for p in frontier:
            for _ in range(max(1, int(rng.expovariate(1 / branching)))):
                if nxt >= n:
                    break
                adj[p].append(nxt)
                new.append(nxt)
                nxt += 1
        frontier = new
    for u in range(n):
        adj[u].extend(rng.randrange(n) for _ in range(cross))
        adj[u].append(0)  # every page links home
    return adj


def bfs(adj):
    dist = {0: 0}
    q = collections.deque([0])
    while q:
        u = q.popleft()
        for v in adj[u]:
            if v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist


def crawl(adj, lat, max_depth, conc, rule):
    if rule == "crawler":
        return crawl_with_frontier(adj, lat, max_depth, conc)
    depth = {0: 0}
    state = {0: "queued"}
    queues = collections.defaultdict(collections.deque)
    queues[0].append(0)
    incomplete = collections.Counter({0: 1})  # queued or in flight, per depth
    stored = {}  # draft: page -> children cut off by the limit
    lowered_in_flight = 0

    def admit(c, d):
        nonlocal lowered_in_flight
        if d > max_depth:
            return False  # cut; not claimed, so a shorter path can still admit it
        if c not in depth:
            depth[c] = d
            state[c] = "queued"
            queues[d].append(c)
            incomplete[d] += 1
            return True
        old = depth[c]
        if rule == "naive" or d >= old:
            return True
        st = state[c]
        depth[c] = d
        if st != "done":
            incomplete[old] -= 1
            incomplete[d] += 1
            if st == "queued":
                queues[d].append(c)  # the old entry goes stale
            else:
                lowered_in_flight += 1
        elif rule == "draft" and c in stored:
            kids = stored.pop(c)
            still_cut = [g for g in kids if not admit(g, d + 1)]
            if still_cut:
                stored[c] = still_cut
        # otherwise: a page already fetched at a non-boundary depth; its links weren't kept,
        # so its descendants keep their stale depths
        return True

    def next_item():
        for d in sorted(queues):
            q = queues[d]
            while q and (state[q[0]] != "queued" or depth[q[0]] != d):
                q.popleft()  # stale entry
            if q:
                if rule == "window":
                    shallowest = min(k for k, v in incomplete.items() if v > 0)
                    if d > shallowest + 1:
                        return None
                return q.popleft()
        return None

    t, seq, events, fetched = 0.0, 0, [], set()
    while True:
        while len(events) < conc and (u := next_item()) is not None:
            state[u] = "in_flight"
            heapq.heappush(events, (t + lat[u], seq, u))
            seq += 1
        if not events:
            break
        t, _, u = heapq.heappop(events)
        fetched.add(u)
        d = depth[u]
        cut = [c for c in adj[u] if not admit(c, d + 1)]
        if cut and rule == "draft":
            stored[u] = cut
        state[u] = "done"
        incomplete[d] -= 1
    return fetched, t, lowered_in_flight


def crawl_with_frontier(adj, lat, max_depth, conc):
    """The crawler's own Frontier and Admission, driven as crawl() drives the rules above:
    pages are keys, dispatched from the frontier while fewer than conc are in flight."""
    frontier = Frontier(max_depth)
    admission = Admission(frontier, Scope.for_url("https://example.com/"))
    admission.admit([("0", "0")], 0)
    t, seq, events, fetched = 0.0, 0, [], set()
    while True:
        while len(events) < conc and (item := frontier.pop(t)) is not None:
            heapq.heappush(events, (t + lat[int(item.key)], seq, item))
            seq += 1
        if not events:
            break
        t, _, item = heapq.heappop(events)
        u = int(item.key)
        fetched.add(u)
        admission.admit([(str(c), str(c)) for c in adj[u]], item.depth + 1)
        frontier.finish(item)
    return fetched, t, 0  # it never lowers an item in flight: it can't


def scenario(seed, n, branching, cross):
    """A seeded site; the --max-depth that cuts through its middle; the pages within it; and
    each page's latency, log-normal with a median of 100 ms and a heavy tail."""
    rng = random.Random(seed)
    adj = make_site(rng, n, branching, cross)
    dist = bfs(adj)
    depths = sorted(dist.values())
    max_depth = depths[len(depths) // 2]
    truth = {v for v, d in dist.items() if d <= max_depth}
    lat = [rng.lognormvariate(math.log(0.1), 1.0) for _ in range(n)]
    return adj, lat, max_depth, truth


def main(seeds):
    families = {  # name: (pages, branching, cross-links per page)
        "sparse sites (3,000 pages, 1 cross-link per page)": (3000, 3.0, 1),
        "denser sites (3,000 pages, 3 cross-links per page)": (3000, 4.0, 3),
    }
    for name, (n, b, x) in families.items():
        wrong = collections.Counter()
        missed = {r: [] for r in ("naive", "draft", "window", "crawler")}
        lowered = 0
        ratio = []
        same_time = 0  # sites where the crawler's frontier took exactly the window's time
        for seed in range(seeds):
            adj, lat, max_depth, truth = scenario(seed, n, b, x)
            span = {}
            for rule, misses in missed.items():
                fetched, span[rule], low = crawl(adj, lat, max_depth, 32, rule)
                assert fetched <= truth
                miss = len(truth - fetched)
                wrong[rule] += miss > 0
                misses.append(miss)
                if rule == "window":
                    lowered += low
            ratio.append(span["window"] / span["draft"])
            same_time += span["crawler"] == span["window"]
        print(f"\n{name}: {seeds} seeded sites, 32 requests in flight, --max-depth = median page depth")
        for rule, m in missed.items():
            print(f"  {rule:<7} runs with pages missed {wrong[rule]:>3}/{seeds}"
                  f"   pages missed per run: mean {statistics.mean(m):5.1f}, max {max(m):4}")
        print(f"  window: in-flight items lowered {lowered}; crawl time / draft crawl time: median "
              f"{statistics.median(ratio):.3f}, p95 {statistics.quantiles(ratio, n=20)[18]:.3f}")
        print(f"  crawler: the same crawl time as window on {same_time}/{seeds} sites")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
