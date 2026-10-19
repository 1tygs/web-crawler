"""Appendix A.11: how candidate concurrency laws behave on simulated single-host servers.

Usage: python bench/limiter_sim.py [SEEDS=3]

Event-driven simulation of 20,000 requests with a 20 ms round trip. Scenarios:
  S0.4, S0.8  one fixed page mix: response times log-normal around 100 ms with spread sigma.
              Ample capacity, so the right answer is to stay at the ceiling
  A           CDN-fronted site, ample capacity. A breadth-first crawl meets cached hub pages first
              (20 ms; 10% uncached at 250 ms), then mostly uncached pages (80%) after 2,000 requests
  A2          the same, but the shift comes after 40 requests, during slow start
  B           small origin: 16 workers at 50 ms per page, so it saturates at ~22 requests in flight
              (320 pages/s x 70 ms). Past that, extra requests only wait in the server's queue
Laws (ceiling 64, start 8, slow start, one decision per round of about `limit` responses):
  draft       DESIGN.md's first draft: cut 10% when the round median > 2x a p10 baseline over the
              last 1,000 samples, else +1 below 1.5x
  errors      only 429/503/timeouts cut (none occur here); slow start exits on latency
  guard       latency never cuts: compare the round median with the median at low load; grow
              below 1.5x, hold above
  guard+probe DESIGN.md §5.3 now (ADR-001): guard, plus a halve-and-measure probe after 10 held rounds
"""
import collections
import heapq
import math
import random
import statistics
import sys


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


class GuardProbeLaw:
    """Latency never cuts. Compare like with like: each round's median against the median at low
    load. Grow while it is below 1.5x that baseline, else hold. After 10 held rounds, probe: halve the
    limit for 2 rounds of requests issued during the probe. If the median falls by 20% or more, the
    queueing was ours: keep the baseline and resume at 3/4 of the old limit. If not, the slowdown
    isn't caused by our load (page mix, other users): re-baseline to the current median and resume."""

    def __init__(self, probe=True):
        self.limit, self.ss, self.base, self.round = 8.0, True, None, []
        self.probe, self.held, self.probing, self.held_median, self.resume = probe, 0, 0, None, None
        self.probe_start = 0.0

    def sample(self, ttfb, issued_at=0.0, now=0.0):
        if self.probing and issued_at < self.probe_start:
            return  # issued before the probe began: says nothing about the lower load
        self.round.append(ttfb)
        if len(self.round) < int(self.limit):
            return
        recent = statistics.median(self.round)
        self.round = []
        if self.base is None:
            self.base = recent
            return
        if self.probing:
            self.probing -= 1
            if self.probing == 0:
                if recent <= 0.8 * self.held_median:
                    self.limit = max(1.0, self.resume * 0.75)
                else:
                    self.base, self.limit = recent, self.resume
            return
        inflated = recent >= 1.5 * self.base
        if self.ss:
            if inflated:
                self.limit, self.ss = max(1.0, self.limit / 2), False
            else:
                self.limit = min(64.0, self.limit * 2)
            return
        if not inflated:
            self.held = 0
            self.limit = min(64.0, self.limit + 1)
            return
        self.held += 1
        if self.probe and self.held >= 10:
            self.held, self.probing, self.held_median, self.resume = 0, 2, recent, self.limit
            self.probe_start = now
            self.limit = max(1.0, self.limit / 2)


def run(law, scenario, total=20000, seed=1):
    """Returns (crawl time, time spent at each integer limit, server queue wait per request)."""
    rng = random.Random(seed)
    rtt = 0.020
    servers = [0.0] * 16 if scenario == "B" else None

    def service(i):
        if scenario in ("A", "A2"):
            shift = 2000 if scenario == "A" else 40
            miss = rng.random() < (0.10 if i < shift else 0.80)
            return rng.lognormvariate(math.log(0.250 if miss else 0.020), 0.3)
        if scenario.startswith("S"):
            return rng.lognormvariate(math.log(0.100), float(scenario[1:]))
        return rng.lognormvariate(math.log(0.050), 0.3)

    t, issued, seq, events = 0.0, 0, 0, []
    lim_time = collections.Counter()
    waits = []
    while True:
        while issued < total and len(events) < int(law.limit):
            arrive, s = t + rtt / 2, service(issued)
            if servers is None:
                start = arrive
            else:
                start = max(arrive, heapq.heappop(servers))
                heapq.heappush(servers, start + s)
            waits.append(start - arrive)
            heapq.heappush(events, (start + s + rtt / 2, seq, t))
            seq += 1
            issued += 1
        if not events:
            break
        done, _, issued_at = heapq.heappop(events)
        lim_time[int(law.limit)] += done - t
        t = done
        law.sample(done - issued_at, issued_at, t)
    return t, lim_time, waits


LAWS = {
    "draft": lambda: DraftLaw(1000),
    "errors": ErrorsOnlyLaw,
    "guard": lambda: GuardProbeLaw(probe=False),
    "guard+probe": lambda: GuardProbeLaw(probe=True),
}


def span(xs, fmt):
    lo, hi = fmt.format(min(xs)), fmt.format(max(xs))
    return lo if lo == hi else f"{lo}–{hi}"


def queued_ms(waits):
    return statistics.mean(waits[len(waits) // 2:]) * 1e3  # second half: after the law has settled


def main(seeds):
    print(f"{seeds} seeds; each cell is the min–max over seeds")
    for sc in ("S0.4", "S0.8", "A", "A2", "B"):
        print(f"\n{sc}\n  {'law':<12} {'crawl (s)':>11} {'x errors':>11} {'mean limit':>11} {'queued in server (ms/request)':>31}")
        base = [run(ErrorsOnlyLaw(), sc, seed=s)[0] for s in range(1, seeds + 1)]
        for name, make in LAWS.items():
            times, ratios, limits, queued = [], [], [], []
            for s in range(1, seeds + 1):
                t, lim_time, waits = run(make(), sc, seed=s)
                times.append(t)
                ratios.append(t / base[s - 1])
                limits.append(sum(k * v for k, v in lim_time.items()) / sum(lim_time.values()))
                queued.append(queued_ms(waits))
            print(f"  {name:<12} {span(times, '{:.0f}'):>11} {span(ratios, '{:.2f}'):>11}"
                  f" {span(limits, '{:.1f}'):>11} {span(queued, '{:.0f}'):>31}")
    print("\ndraft law with other baseline windows (seed 1)")
    for w in (300, 1000, 3000, 10**9):
        label = "all samples" if w == 10**9 else f"last {w:,}"
        print(f"  {label:<12} A: crawl {run(DraftLaw(w), 'A')[0]:5.0f} s   "
              f"B: queued in server {queued_ms(run(DraftLaw(w), 'B')[2]):4.0f} ms/request")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
