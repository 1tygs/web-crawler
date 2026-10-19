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
"""
import collections
import heapq
import math
import random
import statistics
import sys


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


def main(seeds):
    families = {  # name: (pages, branching, cross-links per page)
        "sparse sites (3,000 pages, 1 cross-link per page)": (3000, 3.0, 1),
        "denser sites (3,000 pages, 3 cross-links per page)": (3000, 4.0, 3),
    }
    for name, (n, b, x) in families.items():
        wrong = collections.Counter()
        missed = {r: [] for r in ("naive", "draft", "window")}
        lowered = 0
        ratio = []
        for seed in range(seeds):
            rng = random.Random(seed)
            adj = make_site(rng, n, b, x)
            dist = bfs(adj)
            depths = sorted(dist.values())
            max_depth = depths[len(depths) // 2]  # the limit cuts through the middle of the site
            truth = {v for v, d in dist.items() if d <= max_depth}
            lat = [rng.lognormvariate(math.log(0.1), 1.0) for _ in range(n)]  # median 100 ms, heavy tail
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
        print(f"\n{name}: {seeds} seeded sites, 32 requests in flight, --max-depth = median page depth")
        for rule, m in missed.items():
            print(f"  {rule:<7} runs with pages missed {wrong[rule]:>3}/{seeds}"
                  f"   pages missed per run: mean {statistics.mean(m):5.1f}, max {max(m):4}")
        print(f"  window: in-flight items lowered {lowered}; crawl time / draft crawl time: median "
              f"{statistics.median(ratio):.3f}, p95 {statistics.quantiles(ratio, n=20)[18]:.3f}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
