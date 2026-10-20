# ADR-001: Concurrency control: latency only holds growth; errors cut

**Status:** Accepted · **Date:** 2026-10-20 · **Deciders:** Tiago Delgado · **Design:** [DESIGN.md §5.3](../../DESIGN.md#53-adaptive-concurrency-limiter)

## Context

N1 asks for the highest page rate the server sustains without errors or rising latency, so the crawler adapts how many requests it keeps in flight. On a single host, though, latency is a confounded signal:

- Response times mix cached and uncached, cheap and expensive pages, and the mix shifts as a breadth-first crawl moves through the site.
- With no load from us, the median can sit far above the 10th percentile: 10.6–11.9× on docs.python.org and 3.6–4.8× on the Guardian. On www.python.org it went from 11.1× to 1.2× between two runs, as its CDN cache warmed (DESIGN.md A.12).
- A baseline taken as a low percentile of recent samples forgets what the unloaded server looked like once every sample comes from a queue.

The first draft of §5.3 followed TCP Vegas and Netflix's concurrency-limits in treating rising latency as congestion: it cut the limit by 10% whenever the recent median passed 2× a p10 baseline.

## Decision

- Slow start from 8, doubling each round, up to `--max-concurrency` (default 64).
- Halve on 429, 503, timeouts, resets and refused connections, at most once per round trip, and honour `Retry-After`.
- Latency never cuts. Each round's median is compared with the first round's, which shows the server at low load. Below 1.5× the limit grows by one per round; at or above it, the limit holds.
- After 10 held rounds in a row, probe: halve the limit for two rounds, counting only requests issued during the probe. If the median falls by 20% or more, the queueing was ours, so resume at ¾ of the old limit. Otherwise the slowdown comes from slower pages or other users, so take the current median as the new baseline and resume at the old limit.

## Options considered

From the simulation in DESIGN.md A.11 (`bench/limiter_sim.py`, 3 seeds of 20,000 requests each). Crawl times are relative to option B on the same seed.

| | A: first draft | B: errors only | C: guard, no probe | **D: guard + probe (chosen)** |
|---|---|---|---|---|
| Complexity | Medium | Low | Low | Medium: about 40 lines, plus the simulator as its test |
| Steady, wide spread of response times | 20.1–22.5× | 1.00× | 0.92–1.09× | 0.92–1.09× |
| CDN site, page mix shifts late | 41.4–41.8× | 1.00× | 1.91× | 1.03–1.07× |
| CDN site, page mix shifts during slow start | 42.5–43.4× | 1.00× | 3.25–3.73× | 1.06–1.11× |
| Small origin: time queued in the server per request | 137 ms | 137 ms | 49–59 ms | 37–42 ms |
| Failure mode | Holds healthy sites to 1.4–2.9 requests in flight | Lets a small origin queue for the whole crawl | Stuck below the ceiling after a page-mix shift | Probing costs 1–2% of throughput; thresholds untuned |

## Trade-off analysis

- **A** compares two percentiles of one distribution, so it reads a wide but steady spread of response times as congestion and throttles healthy sites. On a saturated origin its p10 baseline drifts up with the queue, so it ends up where B does.
- **B** is the simplest, and the fastest wherever the server has room. It gives up N1's "without rising latency": a small origin that queues without erroring carries 137 ms of extra wait per request for the whole crawl.
- **C** fixes A's false alarms by comparing like with like, and protects the small origin, but after a page-mix shift it holds below the ceiling for the rest of the crawl.
- **D** adds the probe that tells our own queueing apart from slower pages. It costs a few lines and a simulator, and the simulator has already earned its place: it caught a bug in D's first version, which counted requests issued before the probe began.

## Consequences

- Easier: the law has no I/O, so it's deterministic to test, and the same class runs per host in a multi-host crawler (DESIGN.md §14).
- Harder: there are three thresholds (1.5×, 10 rounds, 20%) to tune, validated in simulation only. `-v` logs every decision so real crawls can check them.
- On CDN-fronted sites with spare capacity the limiter sits at the ceiling, so the ceiling is the politeness setting there. 64 is about ten times the six connections a browser opens per host. It stays the default because it only binds on servers with spare capacity, and the 429/503 and 403 rules catch a server that objects. Revisit it if real crawls run into firewall blocks.
- Revisit the thresholds after the first real crawls, and if a target's latency rises with load but it never returns errors.

## Action items

1. [x] Implement the law as a class with no I/O in `src/web_crawler/limiter.py`. Done in step 3 of v1: `ConcurrencyLaw`, which also cuts once per congestion event (DESIGN.md §5.3).
2. [x] Turn the scenarios in `bench/limiter_sim.py` into `tests/unit/test_limiter.py`, asserting that D stays within 15% of B's crawl time where the server has spare capacity, and under half of B's queueing on the small origin. Done in step 3: the tests load the simulator, which runs the production class, on every seed, and add an origin that answers 429 above 20 requests in flight (DESIGN.md A.11).
3. [ ] Log every decision at `-v`, and check the thresholds against real crawls.
