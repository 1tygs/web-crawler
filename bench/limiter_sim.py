"""Appendix A.11: how candidate concurrency laws behave on simulated single-host servers.

Usage: uv run --group bench python bench/limiter_sim.py [SEEDS=3]

Event-driven simulation of 20,000 requests with a 20 ms round trip. Scenarios:
  S0.4, S0.8  one fixed page mix: response times log-normal around 100 ms with spread sigma.
              Ample capacity, so the right answer is to stay at the ceiling
  A           CDN-fronted site, ample capacity. A breadth-first crawl meets cached hub pages first
              (20 ms; 10% uncached at 250 ms), then mostly uncached pages (80%) after 2,000 requests
  A2          the same, but the shift comes after 40 requests, during slow start
  B           small origin: 16 workers at 50 ms per page, so it saturates at ~22 requests in flight
              (320 pages/s x 70 ms). Past that, extra requests only wait in the server's queue
  T           a throttling origin: S0.4's pages, but a request that arrives while 20 others are being
              served is answered 429 at once. The crawler sends it again as soon as the limit allows
Laws (ceiling 64, start 8, slow start, one decision per round of about `limit` responses):
  draft       DESIGN.md's first draft: cut 10% when the round median > 2x a p10 baseline over the
              last 1,000 samples, else +1 below 1.5x
  errors      only 429/503/timeouts cut (none occur in S, A or B); slow start exits on latency
  guard       latency never cuts: compare the round median with the median at low load; grow
              below 1.5x, hold above
  guard+probe DESIGN.md §5.3 (ADR-001): guard, plus a halve-and-measure probe after 10 held rounds.
              This is the crawler's own class, web_crawler.limiter.ConcurrencyLaw
  every cut   T only: guard+probe, but with every 429 cutting, rather than once per congestion event
"""
import collections
import heapq
import math
import random
import statistics
import sys

from web_crawler.limiter import ConcurrencyLaw, Kind


def p10(xs):
    s = sorted(xs)
    return s[len(s) // 10]


class DraftLaw:
    def __init__(self, window):
        self.limit, self.ss = 8.0, True
        self.long = collections.deque(maxlen=window)
        self.round = []

    def sample(self, ttfb, issued_at=0.0, now=0.0):
        self.long.append(ttfb)
        self.round.append(ttfb)
        if len(self.round) < int(self.limit):
            return
        base, recent = p10(self.long), statistics.median(self.round)
        self.round = []
        if recent > 2.0 * base:
            self.limit = max(1.0, self.limit * 0.9)
            self.ss = False
        elif self.ss:
            self.limit = min(64.0, self.limit * 2)
        elif recent < 1.5 * base:
            self.limit = min(64.0, self.limit + 1)


class ErrorsOnlyLaw:
    def __init__(self):
        self.limit, self.ss, self.base, self.round = 8.0, True, None, []

    def sample(self, ttfb, issued_at=0.0, now=0.0):
        self.round.append(ttfb)
        if len(self.round) < int(self.limit):
            return
        recent = statistics.median(self.round)
        if self.base is None:
            self.base = p10(self.round)
        self.round = []
        if self.ss:
            if recent > 2.0 * self.base:
                self.limit, self.ss = max(1.0, self.limit / 2), False
            else:
                self.limit = min(64.0, self.limit * 2)
        else:
            self.limit = min(64.0, self.limit + 1)


class GuardLaw:
    """ADR-001's option C. Latency never cuts. Compare like with like: each round's median against
    the median at low load. Grow while it is below 1.5x that baseline, else hold."""

    def __init__(self):
        self.limit, self.ss, self.base, self.round = 8.0, True, None, []

    def sample(self, ttfb, issued_at=0.0, now=0.0):
        self.round.append(ttfb)
        if len(self.round) < int(self.limit):
            return
        recent = statistics.median(self.round)
        self.round = []
        if self.base is None:
            self.base = recent
            return
        inflated = recent >= 1.5 * self.base
        if self.ss:
            if inflated:
                self.limit, self.ss = max(1.0, self.limit / 2), False
            else:
                self.limit = min(64.0, self.limit * 2)
        elif not inflated:
            self.limit = min(64.0, self.limit + 1)


class Production:
    """Option D, guard + probe, as the crawler runs it: web_crawler.limiter.ConcurrencyLaw."""

    def __init__(self, ceiling=64):
        self.law = ConcurrencyLaw(ceiling)
        self.decisions = []

    @property
    def limit(self):
        return self.law.limit

    def sample(self, ttfb, issued_at=0.0, now=0.0):
        if (decision := self.law.on_sample(ttfb, issued_at, now)) is not None:
            self.decisions.append(decision)

    def congestion(self, issued_at, now):
        if (decision := self.law.on_congestion(issued_at, now)) is not None:
            self.decisions.append(decision)


class EveryCut(Production):
    """Production's law, but cutting for every 429 rather than once per congestion event."""

    def congestion(self, issued_at, now):
        super().congestion(now, now)  # as if each request had been sent after the last cut


Result = collections.namedtuple("Result", "time lim_time waits throttled")


def run(law, scenario, total=20000, seed=1):
    """Crawl time, time spent at each integer limit, server queue wait per request served, and the
    number of 429s."""
    rng = random.Random(seed)
    rtt = 0.020
    servers = [0.0] * 16 if scenario == "B" else None
    throttle = 20 if scenario == "T" else None
    serving = []  # T: when each request being served finishes, as a heap

    def service(i):
        if scenario in ("A", "A2"):
            shift = 2000 if scenario == "A" else 40
            miss = rng.random() < (0.10 if i < shift else 0.80)
            return rng.lognormvariate(math.log(0.250 if miss else 0.020), 0.3)
        if scenario.startswith("S") or scenario == "T":
            return rng.lognormvariate(math.log(0.100), 0.4 if scenario == "T" else float(scenario[1:]))
        return rng.lognormvariate(math.log(0.050), 0.3)

    t, to_send, issued, seq, events = 0.0, total, 0, 0, []
    lim_time = collections.Counter()
    waits = []
    throttled = 0
    while True:
        while to_send and len(events) < int(law.limit):
            to_send -= 1
            arrive = t + rtt / 2
            if throttle is not None:
                while serving and serving[0] <= arrive:
                    heapq.heappop(serving)
                if len(serving) >= throttle:
                    throttled += 1
                    heapq.heappush(events, (arrive + rtt / 2, seq, t, True))
                    seq += 1
                    continue
            s = service(issued)
            issued += 1
            if servers is None:
                start = arrive
            else:
                start = max(arrive, heapq.heappop(servers))
                heapq.heappush(servers, start + s)
            if throttle is not None:
                heapq.heappush(serving, start + s)
            waits.append(start - arrive)
            heapq.heappush(events, (start + s + rtt / 2, seq, t, False))
            seq += 1
        if not events:
            break
        done, _, issued_at, was_throttled = heapq.heappop(events)
        lim_time[int(law.limit)] += done - t
        t = done
        if was_throttled:
            to_send += 1  # sent again when the limit allows
            law.congestion(issued_at, t)
        else:
            law.sample(done - issued_at, issued_at, t)
    return Result(t, lim_time, waits, throttled)


LAWS = {
    "draft": lambda: DraftLaw(1000),
    "errors": ErrorsOnlyLaw,
    "guard": GuardLaw,
    "guard+probe": Production,
}


def span(xs, fmt):
    lo, hi = fmt.format(min(xs)), fmt.format(max(xs))
    return lo if lo == hi else f"{lo}–{hi}"


def mean_limit(lim_time):
    return sum(k * v for k, v in lim_time.items()) / sum(lim_time.values())


def queued_ms(waits):
    return statistics.mean(waits[len(waits) // 2:]) * 1e3  # second half: after the law has settled


def main(seeds):
    print(f"{seeds} seeds; each cell is the min–max over seeds")
    for sc in ("S0.4", "S0.8", "A", "A2", "B"):
        print(f"\n{sc}\n  {'law':<12} {'crawl (s)':>11} {'x errors':>11} {'mean limit':>11} {'queued in server (ms/request)':>31}")
        base = [run(ErrorsOnlyLaw(), sc, seed=s).time for s in range(1, seeds + 1)]
        for name, make in LAWS.items():
            times, ratios, limits, queued = [], [], [], []
            for s in range(1, seeds + 1):
                result = run(make(), sc, seed=s)
                times.append(result.time)
                ratios.append(result.time / base[s - 1])
                limits.append(mean_limit(result.lim_time))
                queued.append(queued_ms(result.waits))
            print(f"  {name:<12} {span(times, '{:.0f}'):>11} {span(ratios, '{:.2f}'):>11}"
                  f" {span(limits, '{:.1f}'):>11} {span(queued, '{:.0f}'):>31}")
    print(f"\nT\n  {'law':<12} {'crawl (s)':>11} {'mean limit':>11} {'429s':>7} {'cuts':>6} {'limit after first cut':>22}")
    for name, make in (("guard+probe", Production), ("every cut", EveryCut)):
        times, limits, throttled, cuts, ranges = [], [], [], [], []
        for s in range(1, seeds + 1):
            law = make()
            result = run(law, "T", seed=s)
            times.append(result.time)
            limits.append(mean_limit(result.lim_time))
            throttled.append(result.throttled)
            cuts.append(sum(d.kind is Kind.CUT for d in law.decisions))
            first = next(i for i, d in enumerate(law.decisions) if d.kind is Kind.CUT)
            after = [d.limit for d in law.decisions[first:]]
            ranges.append((min(after), max(after)))
        low, high = min(r[0] for r in ranges), max(r[1] for r in ranges)
        print(f"  {name:<12} {span(times, '{:.0f}'):>11} {span(limits, '{:.1f}'):>11}"
              f" {span(throttled, '{:,}'):>7} {span(cuts, '{:,}'):>6} {f'{low}–{high}':>22}")
    print("\ndraft law with other baseline windows (seed 1)")
    for w in (300, 1000, 3000, 10**9):
        label = "all samples" if w == 10**9 else f"last {w:,}"
        print(f"  {label:<12} A: crawl {run(DraftLaw(w), 'A').time:5.0f} s   "
              f"B: queued in server {queued_ms(run(DraftLaw(w), 'B').waits):4.0f} ms/request")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
