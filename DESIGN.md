# Web Crawler: System Design

**Status:** Proposed · **Date:** 2026-10-19, revised 2026-10-20 after an architecture review · **Runtime:** CPython 3.14 · **Scope:** single-host crawler, command line

One asyncio coordinator owns all crawl state and does only cheap work. An adaptive concurrency limit grows until the server shows it is queueing, and halves when the server signals distress. A pool of parser processes does all CPU-heavy work on response bodies that are still compressed.

Every performance number in this document was measured on the development machine or comes from a simulation in `bench/`; methods and raw results are in [Appendix A](#appendix-a-measurements).

---

## 1. Summary

Three things bound the crawler's speed, in this order: the target server's capacity, network latency, and the client's CPU. The design has a lever for each.

| Bound | Lever | Mechanism |
|---|---|---|
| Server capacity | Run close to what the server sustains, without tipping it over | Adaptive concurrency limit: errors halve it, and latency, confirmed by a probe, stops it growing; honours 429/503 and `Retry-After` |
| Network latency | Many requests in flight over few connections; no wasted requests | asyncio + uvloop, aiohttp keep-alive pool, compression, claim-on-discovery dedupe, per-hop redirect dedupe, non-HTML skipped |
| Client CPU | Make per-page work small, then spread it across cores | lexbor (C) parsing, ada-url (C++) URL resolution, process pool fed compressed bytes, constant work per link in the coordinator |

Accuracy comes from matching browser semantics: HTML5 parsing, WHATWG URL resolution, `<base href>`, the final URL after redirects, declarative shadow DOM, and normalization that never merges two distinct resources. Retries mean transient failures don't silently drop pages.

**Measured headroom (Apple M5, CPython 3.14.6):**
- The full parse pipeline costs 0.4–2.8 ms per real page.
- Parsing inline on one core tops out around 610 pages/s.
- Eight parser processes reach about 2,900 pages/s.
- The coordinator's own cost is about 140–230 µs per page (A.3's 110–205 µs plus A.4's 26.5 µs of HTTP), so it saturates only at roughly 4,000–7,000 pages/s.

The target server will be the limit, not the crawler.

---

## 2. Requirements

### 2.1 Functional

- **F1:** `crawl <start-url> [options]` from the command line.
- **F2:** For every page crawled, print the page URL and every URL found on that page.
- **F3:** Crawl only the start URL's host. Links to other domains *and* to subdomains are printed but never fetched.
- **F4:** Finish when every reachable in-scope page has been processed, or a user-set limit is reached.

### 2.2 Non-functional

| ID | Requirement | What it means |
|---|---|---|
| N1 | Speed | The highest page rate the server sustains without errors or rising latency |
| N2 | Accuracy | Every reachable in-scope HTML page is fetched exactly once. Links are reported exactly as a browser resolves them. Nothing is dropped silently |
| N3 | Resource efficiency | Memory grows with unique URLs plus a fixed in-flight allowance. No redundant requests or parsing. Idle workers use no CPU |
| N4 | Robustness | Timeouts, retries, size and decompression caps, trap protection, a clean Ctrl-C that keeps partial output intact |
| N5 | Politeness | robots.txt per RFC 9309, an honest User-Agent, backing off when the server shows distress |
| N6 | Operability | Progress and summary on stderr, a machine-readable output option, meaningful exit codes |

### 2.3 Constraints

- Python only. No crawling frameworks (Scrapy, Playwright, etc.). Libraries are allowed for HTTP, HTML parsing, URL handling and robots.txt parsing.
- Consequence: no JavaScript runs. Links that exist only after client-side rendering are invisible ([§13](#13-limitations-risks-and-what-to-revisit)).

### 2.4 How ambiguous requirements are read

| Question | Decision | Rationale |
|---|---|---|
| What is "the same domain"? | Exact host match after normalization (lowercase, IDNA/punycode). `example.com`, `www.example.com` and `blog.example.com` are three different hosts. The port must match the start URL's, where an omitted port means the scheme's default. On the default ports, `http` and `https` on that host are both in scope; a start URL with any other port keeps to its own scheme | Literal reading of "no subdomains". The scheme isn't part of a domain. Another port is another service |
| The start URL redirects to another host (e.g. apex → `www`) | If the redirect only adds or removes `www.`, crawl the final host instead and say so on stderr. Any other cross-host redirect stops with exit code 3 and prints the URL to use. `--strict-host` treats the `www.` case like the others | The scope is still exactly one host, fixed before any page is fetched, so F3 holds. 5 of 6 bare domains tried redirect to `www` (A.13), so refusing would fail on the most obvious first command |
| What counts as a page? | A 2xx response with HTML content: `text/html` or `application/xhtml+xml`, or sniffed as HTML when the type is missing. Redirects are transparent hops. Errors and non-HTML responses are counted but not printed (JSONL can include them) | A page is a document that can contain links |
| Which URLs are "found on the page"? | Navigational links: `a[href]`, `area[href]`, `iframe[src]`, `frame[src]`, `link[href]` with rel `canonical`/`alternate`/`next`/`prev`, and `<meta http-equiv="refresh">` targets, including inside declarative shadow DOM. `--all-urls` reports every URL-bearing attribute (img/srcset, script, stylesheet, media, …) and every scheme | This is what people mean by "the links on a page". The flag gives the literal reading |
| In what form are found URLs printed? | Absolute and serialized as a browser would request them (WHATWG), fragment removed, de-duplicated within the page in document order, `http(s)` only | Unambiguous and faithful. Fragments never reach the server |
| Output order | Streamed in completion order | Sorting needs the whole crawl in memory and kills live output. `--format jsonl` piped to `sort` gives determinism |

### 2.5 Out of scope

JavaScript rendering, authenticated areas, link extraction from non-HTML files (PDF and so on), resumable crawls, multi-host crawls. [§13](#13-limitations-risks-and-what-to-revisit) gives the trigger for revisiting each, and [§14](#14-beyond-one-host) sketches how the design extends to many hosts.

---

## 3. Why this shape: the performance model

**Little's law sets the shape.** Throughput equals requests in flight divided by latency. For one host at 50–500 ms per response, 300 pages/s at 200 ms needs 60 requests in flight at all times. One thread per request would work, but it costs memory and context switches, and needs locks around shared state. One asyncio event loop handles thousands of sockets on a single thread. Because only one coroutine runs at a time, shared state needs no locks.

**The loop is single-threaded, so CPU per page caps throughput.** These are the measured costs of the full per-page pipeline (decompress → parse → extract → resolve → normalize → render) on real pages:

| Page (decoded size) | Unique links | CPU per page |
|---|---|---|
| python.org (50 KB) | 134 | 0.43 ms |
| docs.python.org (173 KB) | 38 | 1.14 ms |
| MDN (220 KB) | 440 | 1.22 ms |
| Wikipedia (339 KB) | 497 | 1.91 ms |
| The Guardian (1.5 MB) | 299 | 2.80 ms |

Parsing inline on the event loop caps one core at about 610 pages/s on this mix. In practice it's lower, because that core also runs the HTTP work. A CDN-served site at 30 ms latency with 32 requests in flight could deliver about 1,000 pages/s. CPU work therefore has to leave the loop.

**HTTP itself is cheap.** aiohttp on uvloop costs 26.5 µs of CPU per request when it doesn't decompress, and 51.7 µs when it does. So the split is:
- One process does all network I/O and owns all state.
- A pool of processes does all the CPU-heavy work.
- The coordinator passes the body along still compressed. That's less to copy and moves decompression off the loop, which halves the coordinator's cost per request.

**Resulting capacity** (Appendix A):

| Component | Cost | Saturates at |
|---|---|---|
| Coordinator (fetch + IPC + dedupe + write) | ~140–230 µs per page | ~4,000–7,000 pages/s |
| One parser process | ~1.6 ms per page (mix above) | ~600 pages/s |
| Eight parser processes (measured) | — | ~2,900 pages/s |

Few sites will serve one client thousands of pages per second. The crawler's ceiling sits above the server's, so the client should never be the bottleneck.

---

## 4. Architecture

### 4.1 Process and component view

```
┌───────────────────────── coordinator process ─────────────────────────┐
│ one uvloop event loop · owns all mutable state · lock-free            │
│                                                                       │
│  Admission ──► Frontier ───► Dispatcher ───► Fetcher ◄════════════════╪═══► target host
│  dedupe,       depth FIFO,   pipeline slots  aiohttp keep-alive pool, │     (HTTP/1.1)
│  scope,        retry heap,   + adaptive      manual redirects,        │
│  robots,       seen set      limiter ◄────── size caps                │
│  traps                       latency, 429/503, timeouts │             │
│    ▲                                                    │ compressed  │
│    │ in-scope links                                     ▼ body        │
│    └────────── Result handler ◄──────────────── Parse gateway         │
│                  ├──► writer thread ──► stdout     │       ▲          │
│                  └──► stats ──► stderr             │       │          │
└────────────────────────────────────────────────────┼───────┼──────────┘
                                          ParseJob   ▼       │   ParseResult
                                ┌─────────────────────────────────────────┐
                                │ parser pool · ≤ N processes, on demand  │
                                │ decompress → decode → parse → extract → │
                                │ resolve → classify → render  (pure)     │
                                └─────────────────────────────────────────┘
```

| Component | Responsibility | Runs in |
|---|---|---|
| CLI / Config | Parse and validate arguments into a frozen `Config` | coordinator |
| Admission | Decide whether a newly found URL gets crawled: dedupe, scope, trap rules, extension filter, robots, limits | coordinator |
| Frontier | Pending work: depth-ordered FIFO queues, a retry heap keyed by ready time, and the `seen` set | coordinator |
| Dispatcher | Starts a fetch task when a pipeline slot and a limiter permit are both free, inside the depth window under `--max-depth`; detects termination | coordinator |
| Adaptive limiter | Sets how many requests are in flight: errors cut the limit, latency only holds it; `Retry-After` pauses; optional rate cap; circuit breaker | coordinator |
| Transport / Fetcher | One HTTP exchange, headers before body (transport); redirects, triage and size caps (fetcher) | coordinator |
| Parse gateway | Picks the worker count; submits jobs with SIGINT and SIGTERM blocked; after a worker crash, rebuilds the pool and isolates the page that caused it | coordinator |
| Parser workers | Decompress, decode, parse, extract, resolve, classify and render, as a pure function | pool processes |
| Output writer | Writes pre-rendered records to stdout, so a slow reader never blocks the event loop | dedicated thread |
| Stats | Counters, progress line, final summary | coordinator |

**Decisions are made without I/O.** Admission, the frontier and its depth window, accounting, the limiter's law, and the redirect and triage decisions are synchronous classes with no `await`. A thin async layer feeds them events and carries out what they decide. That keeps the hardest logic deterministic to test ([§11](#11-testing-and-verification)) and lets other front ends reuse it ([§14](#14-beyond-one-host)).

### 4.2 Lifecycle of a URL

1. **Discovered.** A parse result lists the page's in-scope links as `(dedupe key, URL)` pairs.
2. **Admitted.** The coordinator checks `key in seen` (~50 ns). If the key is new, it applies the trap rules, the extension filter, robots.txt (Protego, ~3 µs) and the depth limit. If all pass, the key goes into `seen` and the URL into the frontier. This is *claim on discovery*: from now on, no other page, redirect or retry can cause a second fetch.
3. **Dispatched.** The dispatcher takes a *pipeline slot*, which bounds memory (fetches plus the parse queue). It then takes a *limiter permit*, which bounds load on the server, and starts a fetch task. Under `--max-depth` it dispatches only inside the depth window, and under `--max-pages` only while pages printed plus fetches in flight is below the limit ([§5.1](#51-frontier-and-admission)).
4. **Fetched.** A GET is sent with compression enabled and auto-decompression off. When the headers arrive, the limiter records time-to-first-byte and the status is triaged ([§5.4](#54-errors-and-retries)). Non-HTML responses are never parsed, and their bodies are only read when small enough to drain for connection reuse. HTML bodies are streamed up to the size cap. The limiter permit is then released, so parse time never throttles fetching.
5. **Redirected** (on 3xx). `Location` is resolved, scope-checked and admitted as a hop. If it's already claimed, this task stops; that page is fetched by its own task.
6. **Parsed.** The final URL, content-encoding, charset, raw body and depth go to a worker. The worker returns the rendered output record and the page's in-scope links.
7. **Completed.** The record goes to the writer and the links go to admission (step 2). The pipeline slot is released and `outstanding` is decremented.
8. **Terminated.** `outstanding` counts admitted URLs that haven't finished. Children are admitted before the parent decrements, so `outstanding == 0` means the crawl is complete. No polling or idle timeouts are needed.

### 4.3 Concurrency model

- **Coordinator.** A single event loop owns all mutable state: the seen set, frontier, counters and limiter. There's no `await` between "check `seen`" and "add to `seen`", so claims are atomic without locks.
- **Parser pool.** A `ProcessPoolExecutor` with up to `os.process_cpu_count() − 1` workers by default.
  - That count sees CPU affinity but not container quotas: in a 2-CPU container on a 64-core host it would allow 63 workers. So the default is also capped by the cgroup CPU quota (`cpu.max`) when one is set, and `PYTHON_CPU_COUNT` overrides both.
  - The start method is set explicitly: `forkserver` on Linux, `spawn` on macOS and Windows. With either, the executor starts a process only when no idle one exists (verified, A.9). A slow site keeps one or two workers busy; a fast one scales out. `fork` would start every worker up front, and forking a process that has threads isn't safe.
  - An idle worker costs about 40 MiB and no CPU.
  - Workers ignore SIGINT and SIGTERM, so the coordinator handles Ctrl-C once. But a worker started with `spawn` runs for its first 30–50 ms before the pool's initializer can ignore anything, and a Ctrl-C in that window kills it and breaks the whole pool (A.9). So the gateway calls `submit()` with both signals blocked (`signal.pthread_sigmask`): a new worker inherits the mask, and the initializer sets both signals to ignored before unblocking them. With `forkserver`, `submit()` returns only once the worker has been forked, and no loss was seen in 10 trials; blocking the signals costs nothing there either.
- **Writer thread.** Blocking writes to stdout happen off the loop. A stalled reader (`| less`, a slow disk) builds a bounded backlog. Past 64 MiB the dispatcher pauses until it drains, so memory stays bounded and in-flight requests never stall.
- **Backpressure** uses two limits:
  - *Limiter permits* bound requests in flight, i.e. load on the server. A permit is held from request start until the body has arrived.
  - *Pipeline slots* bound items held in memory: fetching, waiting to parse, or parsing. The default is `max_concurrency + 4 × workers`. Without this, a fast server and slow parsing would queue bodies without limit.

---

## 5. Component design

### 5.1 Frontier and admission

- **Order: breadth-first by depth.** The frontier is a list of FIFO queues indexed by depth, and dispatch takes from the shallowest non-empty one. A page links to around 100 others, so BFS keeps the frontier wide and concurrency saturates within two levels. It also reaches important shallow pages first and makes `--max-pages` return a sensible subset.
- **Retry heap.** A `heapq` of `(ready_at, seq, item)`. Items waiting out a backoff hold no permit or slot. The dispatcher wakes at the earliest `ready_at`.
- **Admission checks**, cheapest first. Every rejection is counted and sampled to the log, so nothing is dropped silently.
  1. `key in seen` → drop.
  2. Trap rules (configurable):
     - The URL is longer than 2,048 characters (the sitemap protocol's limit).
     - A path segment is repeated more than three times, as in `/a/b/a/b/a/b/…` from broken relative links on soft-404 sites.
     - A single path has more than 1,000 distinct query strings (calendars, faceted search).
  3. Extension filter (on by default; `--no-skip-extensions` turns it off). It covers images, audio and video, archives, fonts, PDFs, office documents, CSS and JS. Those responses would almost always be discarded unparsed, so skipping them saves a request. The rare exception is a URL such as `/old.pdf` that redirects to an HTML page, which the flag is there for. The link is still printed on the page that contains it.
  4. robots.txt ([§5.7](#57-robotstxt)).
  5. `--max-depth`.

  Rejected keys also go into `seen`, so they aren't re-checked every time they're found. Depth rejections are the exception, so a later, shorter path can still admit them.
- **`--max-pages N`** is enforced when dispatching: a fetch starts only while pages printed plus fetches in flight is below N, so no page is fetched only to be thrown away. A fetch that ends without a page (an error, a non-HTML response, a redirect to a URL already claimed) frees its place. Exactly N records are printed, or fewer if the site is smaller.
- **HSTS upgrade (RFC 6797), after v1.** Once an HTTPS response carries `Strict-Transport-Security`, in-scope `http://` links are upgraded to `https://` before the dedupe check, as browsers do. Legacy `http://` links then cost no redirect round-trip. Until then, redirect-aware dedupe ([§5.6](#56-url-normalization-and-dedupe)) collapses them for one extra request each.
- **Exact depth under `--max-depth`.** Because of concurrency, a page can be reached by a longer path first. A two-level dispatch window makes the result independent of timing:
  - An item at depth *d* is dispatched only when nothing at depth *d*−2 or shallower is still queued, in flight or waiting to retry.
  - A queued item gets its depth lowered if it's found again at a shallower depth.

  A page at depth *d* can only become shallower through a page at depth *d*−2 or less. Once all of those have finished, its depth can no longer change, so it's final by the time the page is dispatched. The crawled set is then exactly the set of pages within N links of the start, and nothing extra is stored.

  In simulation over 600 random sites the window was exact every time, and never had to lower an item already in flight. The first draft's rules (lowering queued items, and keeping the links of pages whose children were cut off) missed pages in 27–34% of runs, because they can't repair a page already fetched at a depth short of the limit: its links weren't kept (A.10). The window adds a median 3–7% to crawl time, and applies only when `--max-depth` is set.

### 5.2 Fetcher and transport

**Transport** opens one HTTP exchange as a handle: status, headers and time to first byte arrive first, and the body is read only if the fetcher asks for it ([§6.2](#62-internal-contracts)). It sits behind a `Transport` protocol so it can be swapped:
- An aiohttp `ClientSession` with `TCPConnector(limit = limit_per_host = max_concurrency + 4, ttl_dns_cache = 300, keepalive_timeout = 30)`. The four extra connections serve requests the dispatcher doesn't count, such as robots.txt refetches and health probes. Happy eyeballs is on by default.
- `auto_decompress=False`, with `Accept-Encoding: gzip, deflate, br, zstd`, exactly the encodings the workers can decode. zstd comes from the 3.14 standard library.
- `Accept: text/html,application/xhtml+xml;q=0.9,*/*;q=0.8` and an honest User-Agent, e.g. `web-crawler/1.0 (+https://github.com/<you>/web-crawler)`.
- Timeouts: connect 10 s, read 20 s, total 60 s.
- TLS verification on; `--insecure` turns it off for dev hosts.
- Cookie jar on, so cookie-gated redirects resolve as they would in a browser.
- Proxies are taken from the environment (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`) and read once. aiohttp's own `trust_env` re-reads them, and `~/.netrc`, on a thread for every request. That costs 162.5 µs of CPU per request against 26.9 µs (A.4), and it would send `~/.netrc` credentials to the crawled host.

**Fetcher** is the crawl logic on top of the transport:
- **Manual redirects.** `allow_redirects=False`, then a loop of up to 10 hops. Each hop is resolved with ada-url, scope-checked, admitted (robots and traps; same depth) and checked for loops. aiohttp's automatic redirects would fetch off-host targets, breaking F3, and would re-fetch pages other tasks have already claimed. The start URL's own redirects are resolved before the scope is fixed ([§2.4](#24-how-ambiguous-requirements-are-read)).
- **Headers-first triage:**
  - HTML: stream the body up to `--max-page-bytes` (default 32 MiB) and flag truncation.
  - Non-HTML: don't read the body. If `Content-Length ≤ 64 KiB`, drain it so the connection can be reused; otherwise close it.
  - Missing `Content-Type`: decompress just the first 512 bytes (zlib, brotli and zstd all take an output cap) and sniff them for the WHATWG HTML signatures. The body is still compressed at this point, so the raw bytes can't be sniffed.
- **Latency sample** is time to response headers. It tracks server queueing without being skewed by body size.

### 5.3 Adaptive concurrency limiter

A fixed concurrency is wrong both ways. Too low wastes capacity (Little's law). Too high just queues work inside the server: latency rises, throughput doesn't, and you risk 429 storms, retries and bans. The limiter searches for the knee continuously, like TCP congestion control.

**Latency is a weak signal on one host.** Response times mix cached and uncached, cheap and expensive pages, and the mix shifts as the crawl moves through the site, so a slower median doesn't by itself mean the server is queueing. Measured one request at a time, with no load from us, the median was 10.6–11.9× the 10th percentile on docs.python.org and 3.6–4.8× on the Guardian in two runs; on www.python.org it fell from 11.1× to 1.2× between the runs, probably because the first run had filled its CDN cache (A.12). So latency only ever stops the limit growing, and a probe checks that a slowdown is really ours before it counts. Only the server's distress signals cut the limit. [ADR-001](docs/adr/001-concurrency-control.md) records the options and the simulation behind this choice.

```text
limit ∈ [1, --max-concurrency (default 64)], starts at 8, in slow start
round = the responses to ≈ limit requests; m = their median ttfb
base  = m of the first round                         # the server at low load

once per round:
    if probing:                                      # only requests issued during the probe count
        after its 2 rounds:
            if m ≤ 0.8 × held_m:  limit = 0.75 × resume      # the queueing was ours
            else:                 base = m; limit = resume   # slower pages or other users
    elif slow_start:
        if m ≥ 1.5 × base:  limit = limit / 2; slow_start = False   # back to the last good limit
        else:               limit = min(2 × limit, max)
    elif m < 1.5 × base:    limit += 1                       # room to grow
    else:                                                    # hold
        after 10 held rounds in a row:
            probing for 2 rounds; held_m = m; resume = limit; limit = limit / 2

on 429 / 503 / timeout / connection reset / refused:
    if no cut within the last RTT:  limit = max(1, limit / 2); slow_start = False
    if Retry-After:                 pause all dispatch until then (capped, default 300 s)
```

- **Why the probe.** Queueing caused by our own load disappears when we halve it; a slowdown caused by slower pages or other users doesn't. The idea is borrowed from BBR's ProbeRTT phase, which drains its own queue to re-measure the path's base delay. Comparing each round's median with the median at low load, rather than with a low percentile, keeps a wide but steady spread of response times from reading as congestion.
- **Evidence (A.11).** In simulation this law stays within about 10% of the ceiling's crawl time when the server has spare capacity. On a small origin that saturates at ~22 requests in flight, it cuts the time each request waits in the server's queue from 137 ms to 37–42 ms, for 1–2% more crawl time. The first draft's law, which cut the limit when the recent median passed 2× a p10 baseline, mistook spread for congestion: it held healthy sites to a mean of 1.4–2.9 requests in flight, 20–43× slower. Its baseline also drifted up once every sample came from a queue, so it never protected the small origin.
- **Untuned.** The thresholds (1.5×, 10 rounds, 20%) are validated in simulation only. Every decision is logged at `-v`, so real crawls can check them.
- A new limit takes effect as permits are released. In-flight requests are never cancelled to shrink it.
- **The ceiling.** It stays at 64 by default. It only binds on servers with spare capacity, and the error signals and the 403 rule ([§5.4](#54-errors-and-retries)) catch a server that objects. It's still about ten times the six connections a browser opens per host, so `--max-concurrency` is the first setting to turn down.
- Optional hard ceilings:
  - `--max-rps` is a token bucket on request starts.
  - A robots.txt `Crawl-delay: d` becomes a 1/d requests-per-second ceiling. This is on by default; `--ignore-robots` turns it off along with the rules.
- **Circuit breaker.** After 20 consecutive connection-level failures (DNS, connect, TLS), dispatch pauses and single probe requests back off from 1 s to 60 s. If the host stays unreachable for 5 minutes, the crawl aborts with exit code 4 and keeps its partial results. That's better than burning every URL's retry budget.

### 5.4 Errors and retries

| Condition | Action |
|---|---|
| DNS failure, connection refused or reset, TLS error, timeout, body cut off mid-transfer | Retry up to 3 times with exponential backoff and full jitter (base 0.5 s, cap 30 s); hard signal to the limiter |
| 429, 503 | Honour `Retry-After` (seconds or HTTP-date), halve the limit, retry. These have their own budget of 5, so throttling doesn't use up the error retries |
| 408, 500, 502, 504 | Retry up to 3 times with backoff |
| Other 4xx/5xx (404, 410, 401, 403, …) | Final: counted, logged at `-v`, optional JSONL error record |
| 20 consecutive 403s after successful pages | Likely a firewall block, not 20 forbidden pages. Pause dispatch and re-request the start URL with backoff. If it recovers, requeue those URLs once; if it's still blocked after 5 minutes, abort with exit code 4 and keep the partial results |
| Redirect loop, more than 10 hops, off-host redirect | Final: recorded with the redirect chain |
| Body over the size cap | Parse the part received and flag `truncated` |
| Invalid compression in a complete body | Final; the result is deterministic, so retrying won't help |
| Worker crash (`BrokenProcessPool`) | Every job in the pool fails, not just the one that crashed (A.9). Rebuild the pool and resubmit them all without using up retries. If it breaks again, rerun the survivors one at a time: the page that crashes on its own gets a parse error, and the rest go through |

Retries go back through the retry heap. Nothing sleeps while holding a permit.

### 5.5 Parser workers

`parse_page(job) -> ParseResult` is a **pure function**: bytes in; bytes and strings out; no I/O; no shared state. That makes it independent of how it runs (a process pool today, threads on a free-threaded build later, inline with `--parse-workers 0`) and easy to unit-test. Static context (scope prefixes, output format, flags) is sent once, through the pool initializer, which also sets SIGINT and SIGTERM to ignored ([§4.3](#43-concurrency-model)).

The pipeline:

1. **Decompress with an output cap:** gzip/deflate via zlib, br via brotli's `output_buffer_limit`, zstd via `compression.zstd` with `max_length`. A small gzip bomb can expand a thousandfold, so the cap applies to what is produced, not just what is downloaded.
2. **Pick the charset in WHATWG order:**
   - Byte-order mark.
   - HTTP `charset` parameter.
   - `<meta charset>` in the first 1,024 bytes.
   - UTF-8 if the bytes validate, else windows-1252.

   WHATWG label aliases apply, e.g. `latin1` and `ascii` map to windows-1252. UTF-8 pages, the vast majority, reach the parser as bytes with no decode copy. lexbor's own detection isn't used: it can't see the HTTP header and has no fallback (verified).
3. **Parse** with selectolax's lexbor engine. Tree construction follows HTML5. Scripting is off, so `<noscript>` content is parsed as markup, which is what a client without JavaScript sees.
4. **Base URL.** Start from the response's final URL after redirects. The first `<base href>` overrides it, resolved against it, per the HTML standard. Resolving against the *requested* URL instead is a classic crawler bug.
5. **Extract** with one CSS selector-list pass, plus recursion into **declarative shadow DOM** (`<template shadowrootmode>`), which browsers render. The content of an ordinary `<template>` is inert and is skipped.
   - On MDN, 11 rendered links (the language switcher) exist only inside shadow roots. A plain lexbor query misses them.
   - lxml or a regex scan would find those, but would also pick up inert template placeholders on other sites.

   Meta refresh `content` is parsed following the HTML standard's refresh steps.
6. **Resolve** each value with ada-url, which implements the WHATWG URL Standard and is the URL parser behind Node.js. It:
   - strips surrounding whitespace and inner tabs and newlines;
   - treats `\` as `/`;
   - IDNA-encodes hosts;
   - percent-encodes spaces and non-ASCII characters;
   - drops default ports and resolves dot segments.

   This costs 0.67 µs per link. `urllib.parse.urljoin` differs from browsers on 10 of 16 tricky inputs (Appendix A.5).
7. **Filter and dedupe.** Keep `http(s)` only, drop the fragment, and dedupe within the page, keeping document order.
8. **Classify scope** with `url.startswith(scope_prefixes)`, e.g. `("https://example.com/", "http://example.com/")`. This is exact because WHATWG serialization is canonical: the host is lowercase punycode, default ports are omitted, and the path always starts with `/`. Look-alikes such as `example.com.evil.net`, `:8443` and `user@` fail the check without being parsed.
9. **Dedupe key** for in-scope links ([§5.6](#56-url-normalization-and-dedupe)).
10. **Render** the output record as bytes: text, or JSONL via orjson. The coordinator just writes it.

### 5.6 URL normalization and dedupe

Each URL has two forms:
- **Display URL:** the WHATWG serialization without its fragment. This form is printed and requested; it's exactly what a browser sends.
- **Dedupe key:** the display URL plus the RFC 3986 §6.2.2 equivalences that WHATWG leaves alone. Percent-encoded unreserved characters are decoded (`%7E` → `~`) and other escapes are uppercased (`%2f` → `%2F`). This step only runs when the URL contains `%`.

| Normalization | Applied? | Why |
|---|---|---|
| Lowercase scheme and host, IDNA, default port, dot segments, percent-encoded spaces and non-ASCII | Yes (WHATWG) | Equivalent by definition |
| Remove fragment | Yes | Never sent to the server |
| Decode unreserved escapes, uppercase hex | Yes (key only) | Equivalent under RFC 3986 |
| `http://` → `https://` | After v1, once HSTS is seen | Browsers do the same. Until then, a redirect settles it |
| Trailing slash, `index.html`, `www.`, path case | No | These can be distinct resources. The common cases redirect and get de-duplicated anyway |
| Sort query parameters | No | Order can matter |
| Strip tracking parameters (`utm_*`, `gclid`, `fbclid`) | After v1, opt-in `--strip-params` | Almost always safe, but it changes URLs |
| Merge by `rel=canonical` | No (reported in JSONL) | A hint, not an equivalence. The fetch has already happened |

**Redirect-aware dedupe.** Every redirect hop is claimed, and pages are de-duplicated by final URL. So `/a` → `/a/` and `http://` → `https://` variants collapse into one printed page without any guessing.

**An exact set, not a Bloom filter.** A false positive means a page silently missed, which N2 rules out. The measured cost is 136 bytes per URL, or 130 MiB per million unique URLs.

### 5.7 robots.txt

- **When.** Rules are per origin (RFC 9309), so the file for every in-scope origin (at most two: `https` and `http` on the default ports) is fetched in parallel before the first page, while the parser pool warms up. Admission then never waits for a file, so it stays synchronous ([§4.3](#43-concurrency-model)).
- **Status handling per RFC 9309:**
  - Follow at least 5 redirects.
  - 2xx: parse the first 500 KiB.
  - 4xx other than 429: allow everything.
  - 429, 5xx or unreachable: retry with backoff, then treat as complete disallow. For the start URL's origin that means exit 3, suggesting `--ignore-robots` for site owners; for the other origin, its URLs are skipped and counted. RFC 9309 doesn't single out 429, but reading "too many requests" as permission would be the wrong way round; Google treats it as a server error too.
- **Parser: Protego.** A small pure-Python library with no dependencies; the Scrapy project maintains it, but it doesn't need Scrapy. It implements longest-match precedence, `*` and `$` wildcards, and `Allow`. `urllib.robotparser` was rejected: it uses first-match semantics, doesn't support wildcards, and is 6–17× slower (measured).
- Groups are matched on the User-Agent's product token. Protego is looser than RFC 9309 here: a group name that starts a token in the User-Agent matches, so a `User-agent: web` group also applies to `web-crawler` (verified, A.6). That only matters when a site names a group after a prefix of the token, so it's accepted and pinned in a test rather than worked around. The file is re-fetched if a crawl runs past 24 hours.

### 5.8 Output

Text (the default) has one block per page, with links indented:

```text
https://example.com/
  https://example.com/about
  https://example.com/blog/
  https://github.com/example

https://example.com/about
  https://example.com/
```

JSONL (`--format jsonl`) has one object per page:

```json
{"url":"https://example.com/blog/","status":200,"depth":1,"links":["https://example.com/","https://example.com/blog/post-1"],"redirected_from":["https://example.com/blog"]}
```

- stdout carries only results. Logs, progress and the summary go to stderr, so `crawl … > out.txt` stays clean.
- Each record is written with one `write()` and is never interleaved. The writer thread flushes every 100 ms and at exit, so `| head`, `tee` and `tail -f` behave as expected. A closed pipe (e.g. `| head`) ends the crawl quietly.
- At thousands of lines per second, the terminal itself becomes the bottleneck. When stdout is a terminal, the progress line suggests redirecting to a file.

### 5.9 Shutdown

- **First SIGINT/SIGTERM.** Dispatching stops and in-flight fetches are cancelled. Parse jobs already submitted finish so their records print; the workers never see the signal ([§4.3](#43-concurrency-model)). Then output is flushed, the summary printed, and the process exits with 130 (SIGINT) or 143 (SIGTERM).
- **Second SIGINT.** Exit immediately; the pool is killed.
- **Normal end.** Shut down the pool, close the session (with a graceful TLS close), flush, print the summary, exit 0.

---

## 6. Interfaces and data model

### 6.1 Command line

```text
crawl URL [options]
```

| Option | Default | Purpose |
|---|---|---|
| `--max-concurrency N` | 64 | Upper bound for the adaptive limiter |
| `--max-rps R` | off | Hard ceiling on requests per second |
| `--parse-workers N\|auto` | auto = CPUs − 1, within any container quota | Cap on parser processes (0 = parse inline) |
| `--max-pages N`, `--max-depth N` | unlimited | Crawl limits, with exact semantics ([§5.1](#51-frontier-and-admission)) |
| `--format text\|jsonl` | text | Output format |
| `-o, --output FILE` | stdout | Output destination |
| `--all-urls` | off | Report every URL-bearing attribute and every scheme |
| `--include-errors` | off | JSONL records for URLs that failed |
| `--ignore-robots` | off | For crawling your own site; also ignores `Crawl-delay` |
| `--strict-host` | off | Refuse a start URL that redirects to another host, even when only `www.` differs |
| `--timeout S`, `--retries N` | 60, 3 | Total timeout per request; retry budget |
| `--max-page-bytes N` | 32 MiB | Size cap per page |
| `--user-agent UA` | `web-crawler/1.0 (+url)` | Also selects the robots.txt group |
| `--insecure`, `--no-skip-extensions` | off | Escape hatches |
| `-v`/`-vv`, `-q` | — | Observability; `-v` also logs the limiter's decisions |

Designed but after v1 ([§15](#15-delivery-plan)): `--strip-params LIST` (query parameters to drop before dedupe, e.g. `utm_*,gclid`), `--respect-nofollow` (don't follow `rel=nofollow` or meta-`nofollow` links, though they're still printed), `--sitemaps` (also seed from the sitemaps listed in robots.txt), `--stats-json FILE` and `--no-cookies`.

A start URL given without a scheme gets `https://`. If connecting or the TLS handshake fails, it's retried once as `http://`, with a note on stderr, so `crawl localhost:8000` works against a plain local server.

**Exit codes:**
- `0`: crawl completed. Errors on individual pages are reported, not fatal.
- `1`: internal error.
- `2`: usage error.
- `3`: couldn't start. The start URL is unreachable, disallowed by robots.txt, or redirects to another host (other than adding or removing `www.`, unless `--strict-host` is set), or its robots.txt is unreachable.
- `4`: aborted because the host became unreachable or kept blocking requests.
- `130`: interrupted.

### 6.2 Internal contracts

```python
@dataclass(slots=True, frozen=True)
class CrawlItem:                 # an admitted URL
    url: str                     # display URL: what is requested and printed
    key: str                     # dedupe key
    depth: int
    attempt: int = 0

class Response(Protocol):        # one HTTP exchange, no redirect handling
    url: str
    status: int
    headers: Mapping[str, str]   # the few the crawler reads
    ttfb: float                  # seconds to response headers
    async def read(self, limit: int) -> tuple[bytes, bool]: ...
                                 # (body, still compressed; truncated at limit)

class Transport(Protocol):       # aiohttp today; an HTTP/2 transport can follow
    def open(self, url: str) -> AbstractAsyncContextManager[Response]: ...
                                 # leaving the context reuses the connection if the
                                 # body was read to the end, and closes it otherwise

@dataclass(slots=True, frozen=True)
class ParseJob:                  # coordinator -> worker (pickled)
    url: str                     # final URL after redirects, i.e. the base URL
    depth: int
    status: int
    content_encoding: str | None
    http_charset: str | None
    body: bytes                  # still compressed
    redirected_from: tuple[str, ...]
    truncated: bool

class ParseResult(NamedTuple):   # worker -> coordinator
    record: bytes                # rendered output, written verbatim
    links: list[tuple[str, str]] # in-scope (key, url) pairs, unique, in document order

def parse_page(job: ParseJob) -> ParseResult: ...   # pure; context set by the pool initializer
```

`Transport.open` hands the fetcher the status and headers before any body is read, so triage stays in the fetcher ([§5.2](#52-fetcher-and-transport)), and a fake transport can drive the fetcher's tests.

---

## 7. Capacity estimate

A worked example: 100,000 pages, 80 KB of HTML on average (about 18 KB gzipped), around 120 unique links per page, a median time-to-first-byte of 150 ms, and the limiter settling at 64.

| Resource | Estimate |
|---|---|
| Throughput | 64 ÷ 0.15 s ≈ 430 pages/s, about 4 minutes in total |
| Bandwidth | 430 × 18 KB ≈ 7.7 MB/s (~62 Mbit/s) |
| Parser CPU | ~0.6 ms/page × 430/s ≈ 0.3 cores, so only a few worker processes ever start |
| Coordinator CPU | ~0.2 ms/page × 430/s ≈ 0.09 cores |
| Memory | Seen set: ~200k in-scope URLs × 136 B ≈ 27 MB. In-flight bodies, still compressed: under 100 slots × 18 KB ≈ 2 MB. About 40 MiB per worker. Well under 300 MB in total |
| Output | 100k pages × 120 links × ~60 B ≈ 720 MB of text, so write to a file rather than a terminal |

On this machine the client becomes the bottleneck only above about 2,900 pages/s with eight parser workers (more cores raise that) or about 4,000–7,000 pages/s at the coordinator.

---

## 8. Key decisions and trade-offs

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| D1 | asyncio + uvloop for I/O | A thread per request; processes for I/O | Thousands of sockets on one thread. State owned by one thread needs no locks. uvloop cut CPU per request by about 10% here |
| D2 | aiohttp over HTTP/1.1 | httpx, niquests, curl_cffi | Lowest CPU per request measured: 51.7 µs, against 108 for curl_cffi, 246 for niquests and 729 for httpx; 26.5 µs without decompression. Mature, with fine control over pooling and streaming. The cost is no HTTP/2, which is why it sits behind `Transport` (D3) |
| D3 | HTTP/2 off by default, with an optional curl_cffi transport | HTTP/2 everywhere | A single host with up to 64 requests in flight gains little: keep-alive amortizes handshakes, and HTTP/1.1 has no head-of-line blocking across connections. HTTP/2 helps when a server caps connections per client. curl_cffi (libcurl + nghttp2, used without browser impersonation) is the cheapest HTTP/2-capable client measured. Its HTTP/2 throughput needs measuring before it's enabled |
| D4 | selectolax (lexbor) | lxml, BeautifulSoup, html.parser, regex | Measured 2.6–3.2× faster than lxml, 12–16× faster than html.parser and 16–45× faster than BeautifulSoup. lexbor implements the full HTML5 parsing algorithm, including tree construction; libxml2's HTML parser doesn't. Regex is simply wrong: entities like `&amp;`, comments, scripts and quoting all break it |
| D5 | ada-url (WHATWG) | urllib.parse, yarl | Matches browsers exactly and was the fastest measured: 0.67 µs per link, against 1.86 µs (urljoin) and 1.20 µs (yarl). urllib.parse differs from browsers on 10 of 16 tricky inputs. yarl follows RFC 3986: it maps `\` to `%5C` and decodes `%2F` in queries |
| D6 | Process pool for parsing, fed compressed bytes | Inline parsing; free-threaded threads; subinterpreters; sharding the whole crawl across processes | Inline parsing caps at about 610 pages/s per core. Free-threading is blocked today because ada-url and brotli publish no cp314t wheels. Subinterpreters need C-extension support that isn't established. Sharding needs distributed dedupe and rate control, and almost every link found would have to cross to the process that owns it |
| D7 | `ProcessPoolExecutor`, with submits under a blocked signal mask and crash isolation around it | A custom pipe-based pool with batching | The measured coordinator overhead of 110–205 µs per page (140–230 µs with HTTP) puts its ceiling at roughly 4,000–7,000 pages/s, far above realistic server rates. Its two sharp edges, a Ctrl-C while a worker starts and one crash failing every job, are handled around it (A.9). Revisit only if profiling shows the coordinator saturating |
| D8 | Decompress in the workers | aiohttp auto-decompress | Halves coordinator CPU per request (51.7 → 26.5 µs). Lets the zip-bomb cap apply to decoded size |
| D9 | An exact in-memory `set` | Bloom or cuckoo filter; an on-disk key-value store | False positives silently drop pages. 130 MiB per million URLs is affordable; disk only pays off beyond about 10M URLs |
| D10 | BFS by depth, with a two-level dispatch window under `--max-depth` | DFS; heuristic priority; lowering depths and keeping cut-off pages' links | Wide frontier, predictable `--max-pages` subsets, exact depth semantics. DFS dives into traps first. The window was exact on 600 of 600 simulated sites; keeping links missed pages on 27–34% of them (A.10) |
| D11 | Manual redirects | Library-followed redirects | Per-hop scope check (F3) and dedupe. No duplicate fetches when many URLs redirect to one |
| D12 | Adaptive concurrency: errors cut, latency only holds, a probe confirms ([ADR-001](docs/adr/001-concurrency-control.md)) | A fixed number; cutting on latency against a p10 baseline; errors only | Follows the server's capacity as it changes and avoids 429 storms. Cutting on latency ran healthy sites 20–43× slower and never protected a saturated one; errors alone let a small origin queue 137 ms per request (A.11) |
| D13 | Respect robots.txt by default | Ignore it | Correct behaviour on other people's sites. Owners can opt out |
| D14 | Sitemaps opt-in | On by default | Sitemaps add orphan pages, which changes what "crawl" means. The speed gain is only one or two round-trips at start-up, since BFS fan-out saturates concurrency within two levels |
| D15 | Skip non-HTML by extension; abort on `Content-Type` | HEAD before GET; fetch everything | HEAD doubles the requests for every HTML page. Skipping changes nothing in the output |
| D16 | Streamed output | Sorted output | Sorting holds everything in memory and delays all output to the end. Use JSONL and `sort` when determinism matters |
| D17 | A writer thread for stdout | Writing from the event loop | A slow reader would otherwise block the loop and time out every in-flight request |
| D18 | Follow a start-URL redirect that only adds or removes `www.` | Exit 3 on any cross-host start redirect | The scope is still one host, fixed before the first page. 5 of 6 bare domains tried redirect to `www` (A.13) |
| D19 | Fetch every in-scope origin's robots.txt up front | Fetch on first use and hold that origin's URLs | Two requests at most, and admission never has to wait, so it stays synchronous |

**Micro-optimizations rejected:**
- Parsing while the body streams: it saves at most one round-trip at start-up, and selectolax doesn't support it.
- Fetching the start page in parallel with robots.txt: that would fetch before knowing the page is allowed.
- Parsing small pages inline: it would save about 0.1 ms of IPC latency per page, but it moves the parse itself (0.4 ms even for python.org) onto the coordinator's single core, the scarcer resource. `--parse-workers 0` keeps the inline path for tests and tiny crawls.

---

## 9. Technology choices

| Concern | Choice | Version evaluated |
|---|---|---|
| Runtime | CPython | 3.14.6 (3.15 is at release candidate) |
| Event loop | uvloop, started with `asyncio.run(main(), loop_factory=uvloop.new_event_loop)` since the event-loop policy API is deprecated in 3.14; the default asyncio loop on Windows | 0.23.0 |
| HTTP | aiohttp | 3.14.5 |
| HTML parsing | selectolax with the lexbor engine (1.0 removed the old Modest engine) | 1.0.1 |
| URL parsing | ada-url | 4.0.0 |
| robots.txt | protego | 0.7.0 |
| Brotli, zstd | brotli; `compression.zstd` from the standard library | 1.2.0; 3.14 |
| JSON output | orjson | 3.13.0 |
| CLI | argparse (standard library) | — |
| Tooling | uv (lockfile), ruff, mypy `--strict`, pytest + pytest-asyncio, hypothesis | — |

Python 3.14 or later is required, because `compression.zstd` is in its standard library. The multiprocessing start method is set explicitly ([§4.3](#43-concurrency-model)) rather than relying on 3.14's new Linux default.

`uv run` installs the interpreter automatically, and the Docker image needs no local Python at all.

---

## 10. Observability

- **Progress line** on stderr, only when it's a terminal, refreshed four times a second:
  `pages 48,210 · 431/s · in-flight 64/64 · queued 12,904 · retrying 3 · errors 17 · 7.6 MB/s · workers 2/9`
- **Final summary** on stderr:
  - pages printed, links found, unique URLs;
  - status code histogram and errors by class;
  - policy rejections by rule (robots, traps, extension), with examples;
  - time-to-first-byte p50/p95/p99;
  - bytes on the wire vs decoded;
  - duration, average and peak pages/s, limiter range, peak RSS.
- `-v` logs failures, rejections and the limiter's decisions; `-vv` logs every request. After v1, `--stats-json FILE` writes the summary for scripts.
- Policy decisions are always counted and sampled. An accurate crawl is one whose omissions you can audit.

---

## 11. Testing and verification

| Layer | What | How |
|---|---|---|
| Unit: URLs | Resolution matches browsers on the inputs the crawler relies on; the dedupe key is idempotent and never merges distinct URLs; scope edge cases | A pinned subset of WHATWG `urltestdata.json` (ada-url runs the full suite upstream); hypothesis property tests; table tests for `www`, subdomains, IDN, case, trailing dot, ports, userinfo, http vs https |
| Unit: parser | Golden fixtures | Relative and absolute `<base>`, entities, whitespace, `\`, shadow DOM vs inert template, `<noscript>`, meta refresh variants, frames, charset via HTTP/meta/BOM and undeclared windows-1252, truncated and malformed HTML, every content-encoding, decompression bombs |
| Unit: frontier | Each URL is dispatched at most once; the crawl ends exactly when nothing is outstanding; depths are exact inside the window | Admission, the depth window and accounting have no I/O ([§4.1](#41-process-and-component-view)), so hypothesis can drive them with generated completion orders |
| Unit: limiter | Within 15% of the ceiling's crawl time when the server has spare capacity; under half the unthrottled queueing on a small origin; halves on 429; honours `Retry-After`; one cut per congestion event | The simulator behind ADR-001 (`bench/limiter_sim.py`) with a fake clock: a narrow and a wide steady spread of response times, a page-mix shift late and during slow start, and a small origin that queues |
| Unit: robots | The crawler's own handling: fetching every origin up front, each status class including 429, redirects, re-fetching after 24 hours | Table tests, plus a few pinned Protego behaviours (longest match, wildcards, group selection). Google's test suite isn't run against Protego, which matches groups more loosely ([§5.7](#57-robotstxt)) |
| Integration | Output matches ground truth exactly; each URL is fetched at most once | An in-process aiohttp server serving sites with known link graphs: the scenarios below, plus seeded random graphs with random latencies, compared against a breadth-first search with and without `--max-depth`. The server's request log proves there are no double fetches |
| Fault injection | Recovery without losing pages | Connection resets mid-body, truncated gzip, `kill -9` on one worker of a busy pool (only its page fails), Ctrl-C mid-crawl and while workers are starting (no partial records; summary printed), a run of 403s mid-crawl |
| Performance | No regressions | A local server with 50 ms of injected latency and 50k pages, tracking pages/s, coordinator CPU per page and peak RSS in CI. `bench/` holds the scripts behind this document |

The integration test sites cover:
- cycles, redirect chains and loops, off-host redirects, a start URL on the apex that redirects to `www` (followed) or to another host (exit 3), and a start URL without a scheme on `localhost`;
- 429/503 with `Retry-After`, intermittent errors, slow pages and very large pages;
- every compression encoding, non-HTML responses and robots.txt rules;
- traps (an infinite calendar, soft-404 `/a/a/a/…`) and duplicate URL variants.

---

## 12. Project layout

```text
web-crawler/
├── pyproject.toml            # uv-managed; console script: crawl = web_crawler.cli:main
├── Makefile                  # test, lint, typecheck, bench, demo
├── Dockerfile                # runs the crawler without a local Python
├── .pre-commit-config.yaml   # ruff and mypy before each commit
├── .github/
│   ├── workflows/ci.yml      # ruff, mypy --strict, pytest on Linux and macOS, coverage gate
│   └── dependabot.yml
├── docs/adr/                 # architecture decision records
├── src/web_crawler/
│   ├── __main__.py           # python -m web_crawler
│   ├── cli.py                # argparse → Config; exit codes
│   ├── config.py             # frozen Config dataclass and validation
│   ├── engine.py             # async layer: dispatcher, termination, signals
│   ├── frontier.py           # depth queues and window, retry heap, seen set, admission (no I/O)
│   ├── fetcher.py            # redirects, triage, size caps
│   ├── transport.py          # Transport protocol and the aiohttp implementation
│   ├── limiter.py            # the limit law (no I/O), Retry-After pause, rate cap, circuit breaker
│   ├── pool.py               # parse gateway: worker count, masked submits, crash isolation
│   ├── robots.py             # RFC 9309 fetching and Protego policy, per origin
│   ├── parse.py              # pure parse_page(); runs in the workers
│   ├── urls.py               # dedupe key, scope prefixes, trap rules, extension filter
│   ├── output.py             # writer thread, text/JSONL, broken-pipe handling
│   └── stats.py              # counters, progress line, summary
├── tests/{unit,integration,fixtures}/
└── bench/                    # the measurement and simulation scripts behind Appendix A
```

Each commit is small and lands with its tests, so the history shows how the crawler was built ([§15](#15-delivery-plan)).

---

## 13. Limitations, risks and what to revisit

**Known limitations**
- **No JavaScript.** Links created client-side aren't seen. Server-rendered and statically generated sites are unaffected; single-page apps are.
- **Soft 404s.** A 200 response for a missing page looks like a real page. Trap rules limit the damage.
- **Legacy-encoded pages.** Browsers percent-encode non-ASCII characters in the *query* using the document's encoding; ada-url always uses UTF-8. This only matters on non-UTF-8 pages with raw non-ASCII in query strings. If it comes up, the fix is to pre-encode the query in the document's charset.
- **Trap thresholds.** The defaults give up a little completeness (e.g. listings with more than 1,000 query variants) in exchange for guaranteed termination. Every cut is reported in the summary.
- **Printed depth.** Without `--max-depth`, the `depth` field is the shortest path known when the page was printed. With it, depths are exact.
- **Limiter thresholds.** The law in [§5.3](#53-adaptive-concurrency-limiter) is validated in simulation only, and its thresholds are untuned. `-v` logs every decision so real crawls can check them.
- **Firewall challenges.** A block served as 403s is caught by the rule in [§5.4](#54-errors-and-retries). A challenge page served with status 200 looks like an ordinary page with few links.

**What to revisit as it grows**

| Trigger | Change |
|---|---|
| More than ~10M URLs, or crawls that must survive restarts | A disk-backed seen set and frontier (SQLite in WAL mode, or LMDB) with an in-memory hot set; periodic checkpoints |
| Coordinator CPU above ~70% in profiles | Replace `ProcessPoolExecutor` with a pipe-based pool that batches jobs; move the trap checks into the workers |
| ada-url and brotli publish free-threaded (cp314t) wheels | A thread pool on a free-threaded build, one parser per thread (lexbor parsers aren't thread-safe). No pickling or IPC |
| The target caps connections per client | Enable the HTTP/2 transport, after measuring it |
| JavaScript-heavy targets | A rendering transport behind `Transport` (outside this exercise's constraints) |
| More than one host | Per-host queues and limiters, then hosts sharded across machines, with a service in front instead of a command line ([§14](#14-beyond-one-host)) |
| Repeated crawls of the same site | Conditional requests (ETag, Last-Modified) against saved state, for incremental crawls |

---

## 14. Beyond one host

The brief asks how the crawler would extend to many domains at once, and whether a command line still fits. In short: the engine becomes a library with the CLI as one front end; within one process, politeness state becomes per host; across machines, hosts are sharded so each host's state lives in exactly one place; and job control moves to a service.

### 14.1 The engine as a library

- `crawl(spec: CrawlSpec, sink: Sink) -> CrawlStats`. `CrawlSpec` is today's `Config` plus a scope policy. `Sink` is a protocol with `write(record)` and `close()`: text and JSONL now; Kafka, object storage or a database later.
- The CLI shrinks to argument parsing, a stdout sink and exit codes.
- The parts without I/O (`parse_page`, the URL rules, the robots policy, the limiter's law, admission) are reused unchanged by every front end below. That's the payoff of keeping them free of I/O ([§4.1](#41-process-and-component-view)).

### 14.2 Many hosts in one process

| Today, one host | Many hosts |
|---|---|
| One frontier, ordered by depth | A queue per host, plus a heap of hosts keyed by when each may next be requested (Mercator's back queues). The dispatcher serves the earliest ready host |
| One limiter and one robots policy | One of each per host, with its own `Retry-After` pause and circuit breaker. Politeness is per host, or per IP address for shared hosting, never global |
| Scope: one host's prefixes | A scope policy per job: a list of hosts, a registrable domain, or a budget of off-site hops |
| Connections capped by the limiter's ceiling | Each host's limiter sets its connections; global caps bound total connections, memory and the parse queue |
| A seen set in memory | The same set, now growing with every host, so the disk-backed store in [§13](#13-limitations-risks-and-what-to-revisit) is needed sooner |

A slow or throttled host mustn't hold global slots: the heap only offers hosts whose limiter has room.

### 14.3 Many machines

- **Shard by host.** Hash the registrable domain (from the Public Suffix List) to a worker. A host's limiter, robots cache, frontier and seen set all live on that worker, so no distributed rate limiting or global dedupe is needed.
- **Route links to their owner.** A link to a host owned elsewhere goes through a queue partitioned by host (Kafka, for example), batched to keep traffic down.
- **Keep state durable.** Each shard keeps its frontier and seen set on disk (SQLite or LMDB, [§13](#13-limitations-risks-and-what-to-revisit)), so a worker can restart and resume. Rebalancing moves whole hosts between workers.
- **Write results to storage**, such as object storage or a table, through a `Sink`, not to a terminal.

### 14.4 Why not a command line

A command line suits one person crawling one site now. A multi-site crawler outgrows it:

- crawls outlive a terminal session and must survive restarts;
- people need to submit, list, inspect, cancel and resume jobs, and watch progress;
- several users bring quotas, priorities, authentication and an audit trail;
- output runs to gigabytes and feeds other systems;
- operators need metrics, alerts and structured logs, not a progress line.

| Concern | Better fit |
|---|---|
| Job control | An HTTP API: `POST /crawls` with a spec returns an id; `GET /crawls/{id}` gives status and stats; `DELETE /crawls/{id}` cancels. Results stream as NDJSON or go to storage |
| Execution | Stateless API servers in front; crawl workers fed from a queue; a scheduler for recurring crawls |
| State | A durable frontier and seen set per shard; job metadata in Postgres |
| Results | Object storage or a table, one `Sink` per destination |
| Observability | Metrics per host and per job (Prometheus), structured logs, alerts on error rates and stalled jobs |

The command line stays, both as today's local single-site tool and as a thin client of the API (`crawl submit …`, `crawl status …`).

---

## 15. Delivery plan

v1 ships the core and the differentiators, each with its tests, so the code a reviewer reads is all built and all tested. The build order keeps the repository passing CI and doing something useful after every step:

1. **Walking skeleton.** CLI → frontier → fetcher → inline parse → text output, against the in-process test server.
2. **Accuracy.** WHATWG resolution, `<base href>`, charsets, declarative shadow DOM, redirect-aware dedupe, robots.txt.
3. **Speed.** The process pool (masked submits, crash isolation), compressed bodies, and the limiter with its simulator.
4. **Robustness.** Retries, `Retry-After`, the circuit breaker, the 403 rule, trap rules, size caps, shutdown.
5. **Limits and output.** `--max-pages`, the `--max-depth` window, JSONL, progress and summary.

Designed here but after v1: the HSTS upgrade ([§5.1](#51-frontier-and-admission)), `--strip-params`, `--respect-nofollow`, `--sitemaps`, `--stats-json`, `--no-cookies` and the HTTP/2 transport (D3). `--ignore-crawl-delay` is folded into `--ignore-robots`.

---

## Appendix A: Measurements

**Environment:** Apple M5 (10 cores), 24 GB RAM, macOS (Darwin 25.6), CPython 3.14.6. Library versions are listed in [§9](#9-technology-choices).

**Method:** five real pages were fetched once, then served from disk or from a local server. Figures are medians of repeated runs.

**Reproducing:** run the scripts in `bench/`. The fixture pages aren't committed because they're third-party content; fetch your own with `curl --compressed`.

**A.1 Link extraction only (`a[href]`), median time per page**

| Page (decoded size) | lexbor | lxml | html.parser | bs4 + lxml | bs4 + html.parser |
|---|---|---|---|---|---|
| The Guardian (1,568 KB) | **1.79 ms** | 4.87 | 24.70 | 29.28 | 44.99 |
| Wikipedia (339 KB) | **0.87** | 2.76 | 10.66 | 19.68 | 25.90 |
| MDN (220 KB) | **0.48** | 1.24 | 6.74 | 12.28 | 17.43 |
| docs.python.org (173 KB) | **0.65** | 1.75 | 10.23 | 22.08 | 28.92 |
| python.org (50 KB) | **0.18** | 0.48 | 2.41 | 5.10 | 6.76 |

All five parsers found the same number of `<a href>` elements except on MDN. There lexbor found 537 and the others 548; the 11 missing links are inside declarative shadow roots. [§5.5](#55-parser-workers) step 5 recovers them: the count becomes 548, for 0.04 ms more per page.

**A.2 Full worker pipeline per page (decompress, parse, extract, resolve, normalize, render)**

| Page | Unique links | gzip | br | zstd | uncompressed |
|---|---|---|---|---|---|
| The Guardian | 299 | 2.80 ms | 3.11 | 2.72 | 2.37 |
| Wikipedia | 497 | 1.91 | 2.05 | 1.88 | 1.73 |
| MDN | 440 | 1.22 | 1.28 | 1.22 | 1.13 |
| docs.python.org | 38 | 1.14 | 1.18 | 1.15 | 1.09 |
| python.org | 134 | 0.43 | 0.44 | 0.43 | 0.40 |

**A.3 Coordinator with and without the parser pool**

Mixed pages from A.2 with gzip bodies; the coordinator submits, receives, dedupes and writes.

| Parser processes | Pages/s | Coordinator CPU per page |
|---|---|---|
| 0 (inline) | 613 | 1,629 µs |
| 1 | 591 | 114 µs |
| 2 | 1,154 | 111 µs |
| 4 | 2,030 | 181 µs |
| 8 | 2,915 | 205 µs |

**A.4 HTTP client cost**

Loopback, 64 concurrent requests, a 52 KB page served gzipped (9.7 KB), 20,000 requests.

| Client | Requests/s | Client CPU per request |
|---|---|---|
| aiohttp 3.14.5 + uvloop, `auto_decompress=False` | 37,635 | **26.5 µs** |
| aiohttp + uvloop | 19,282 | 51.7 µs |
| aiohttp, default asyncio loop | 17,557 | 56.8 µs |
| curl_cffi 0.16.3 | 8,634 | 107.7 µs |
| niquests 3.21.2 | 4,062 | 245.9 µs |
| httpx 0.28.1 | 950 | 729.2 µs |

Requests/s on loopback also depends on the local test server (six processes). CPU per request is the figure to compare.

`trust_env=True`, which takes proxy settings and `~/.netrc` credentials from the environment, adds two thread round trips to every request in aiohttp 3.14.5: one to read `~/.netrc`, one to look up the proxy. Measured the same way on 2026-10-20 (`client_bench.py` with `+env`), with a synthetic 48 KB page (3.8 KB gzipped), median of three runs:

| Client | Requests/s | Client CPU per request |
|---|---|---|
| aiohttp + uvloop, `auto_decompress=False` | 37,006 | 26.9 µs |
| The same with `trust_env=True` | 8,415 | 162.5 µs |
| The same with `trust_env=True` and `NO_PROXY=*` | 13,719 | 83.1 µs |

With `NO_PROXY=*` set, Python doesn't read macOS's system proxy settings, which is closer to what Linux pays. The transport reads the proxy variables once instead ([§5.2](#52-fetcher-and-transport)).

**A.5 URL resolution against the base `https://example.com/dir/page.html`**

| href | `urllib.parse.urljoin` | yarl | ada-url (same as browsers) |
|---|---|---|---|
| `"  /path with space  "` | `https://example.com/path with space  ` | `https://example.com/path%20with%20space%20%20` | `https://example.com/path%20with%20space` |
| `\about` | `https://example.com/dir/\about` | `https://example.com/dir/%5Cabout` | `https://example.com/about` |
| `//EXAMPLE.com:443/x` | `https://EXAMPLE.com:443/x` | `https://example.com/x` | `https://example.com/x` |
| `https://bücher.example/straße` | unchanged (raw Unicode) | `https://xn--bcher-kva.example/stra%C3%9Fe` | `https://xn--bcher-kva.example/stra%C3%9Fe` |
| `/café` | `https://example.com/café` | `https://example.com/caf%C3%A9` | `https://example.com/caf%C3%A9` |
| `?q=a b` | `…/page.html?q=a b` | `…/page.html?q=a+b` | `…/page.html?q=a%20b` |
| `HTTP://Example.COM/A` | unchanged | `http://example.com/A` | `http://example.com/A` |
| `java` + newline + `script:alert(1)` | returned as is, newline included | `javascript:alert(1)` | `javascript:alert(1)` |
| `http://example.com:80/` | port kept | `http://example.com/` | `http://example.com/` |
| `https://example.com\evil.com/` | unchanged | raises `ValueError` | `https://example.com/evil.com/` |
| `/a?b=%2f&c=%2F` | unchanged | `/a?b=/&c=/` (escapes decoded) | unchanged |

Of the 16 inputs tested, `urljoin` differs from browsers on 10 and yarl on 6. Some differences produce wrong URLs; others produce false duplicates.

Speed over 2,228 real hrefs: ada-url `join_url` 0.67 µs per link, yarl 1.20 µs, `urljoin` 1.86 µs (2.87 µs including the `urlsplit` needed to get the host).

**A.6 robots.txt matching**

Protego takes 2.7–3.4 µs per check; `urllib.robotparser` takes 19–46 µs. Both were run on Wikipedia's 28 KB file and The Guardian's 3 KB file, and made identical decisions on these samples.

Group matching, on Protego 0.7.0: with groups named `crawler`, `web` and `*`, the product token `web-crawler` gets the `web` group's rules. Protego accepts a group name that starts a token in the User-Agent; RFC 9309 matches the product token as a whole.

```python
from protego import Protego
p = Protego.parse("User-agent: crawler\nDisallow: /a\n\nUser-agent: web\nDisallow: /b\n\nUser-agent: *\nDisallow:\n")
[p.can_fetch(f"https://example.com/{x}", "web-crawler") for x in "abc"]   # [True, False, True]
```

**A.7 Memory**

| Item | Cost |
|---|---|
| Seen set | 136 B per URL (62-character average), i.e. 130 MiB per million |
| Frontier entry | 72 B |
| Parser worker | ~40 MiB RSS |

**A.8 Free-threaded wheels on PyPI for CPython 3.14t (October 2026)**

| Status | Packages |
|---|---|
| Available | selectolax; aiohttp and its dependencies multidict, yarl, propcache and frozenlist; uvloop |
| Missing | ada-url, brotli, orjson |
| Pure Python | protego |

**A.9 Process pool behaviour** (`bench/pool_check.py`)

`ProcessPoolExecutor(max_workers=8)` on macOS, with jobs that sleep 0.3–0.5 s and an initializer that ignores SIGINT. Ctrl-C is simulated by sending SIGINT to the process group, as a terminal does.

| Check | `spawn` | `forkserver` |
|---|---|---|
| Workers started | 1 job → 1; 4 jobs one at a time → still 1; 8 at once → 8 | The same |
| One worker of a busy pool dies (16 jobs: 8 running, 8 queued) | 16 of 16 futures fail with `BrokenProcessPool`, and every later `submit()` raises it too | The same |
| SIGINT 5–30 ms after starting 8 workers | All 8 jobs lost in 8 of 8 trials, with KeyboardInterrupt tracebacks on stderr | No loss in 8 trials |
| SIGINT 50 ms after | No loss (2 trials) | No loss (2 trials) |
| The fix: SIGINT blocked around `submit()`, then ignored and unblocked in the initializer | No loss in 10 of 10 trials (5–50 ms) | No loss in 10 of 10 trials |

**A.10 `--max-depth` rules** (`bench/depth_sim.py`)

An event-driven simulation of a shallowest-first crawl with 32 requests in flight over seeded random sites: a tree of 3,000 pages grown by a branching process, plus random cross-links and a link home on every page. Page latency is log-normal (median 100 ms, σ = 1). `--max-depth` is set to the median page depth, and a breadth-first search gives the ground truth. 300 sites per family.

| Rule | Sparse (1 cross-link per page): runs with pages missed | Mean pages missed (worst) | Denser (3 cross-links per page): runs with pages missed | Mean pages missed (worst) |
|---|---|---|---|---|
| Depth fixed at first discovery (Scrapy's `DEPTH_LIMIT`) | 295 / 300 | 56.5 (877) | 299 / 300 | 75.2 (417) |
| First draft: lower queued and in-flight items; keep and re-admit cut-off pages' links, recursively | 82 / 300 | 12.4 (858) | 102 / 300 | 6.4 (167) |
| Two-level window ([§5.1](#51-frontier-and-admission)) | 0 / 300 | 0 | 0 / 300 | 0 |

The window never had to lower an item already in flight. Its crawl time relative to the first draft: median 1.065 and p95 1.255 on sparse sites; median 1.028 and p95 1.162 on denser ones.

**A.11 Concurrency laws** (`bench/limiter_sim.py`)

An event-driven simulation of 20,000 requests with a 20 ms round trip, over 3 seeds. Each cell is the range over seeds; "× errors only" compares crawl time with the errors-only law on the same seed. Ceiling 64, start 8.

- **Steady, narrow:** response times log-normal around 100 ms, σ = 0.4; ample capacity.
- **Steady, wide:** the same with σ = 0.8.
- **CDN, late shift:** cached pages at 20 ms with 10% uncached at 250 ms, then 80% uncached after 2,000 requests; ample capacity.
- **CDN, early shift:** the same shift after 40 requests, during slow start.
- **Small origin:** 16 workers at 50 ms per page, saturating at ~22 requests in flight.

| Scenario | Law | Crawl time | × errors only | Mean limit | Queued in the server, 2nd half (ms/request) |
|---|---|---|---|---|---|
| Steady, narrow | all four | 41–42 s | 0.98–1.00 | 61.8–63.4 | 0 |
| Steady, wide | first draft | 1,094–1,160 s | 20.1–22.5 | 2.7–2.9 | 0 |
| | errors only | 51–55 s | 1.00 | 58.4–61.9 | 0 |
| | guard, with or without probe | 50–56 s | 0.92–1.09 | 56.9–63.2 | 0 |
| CDN, late shift | first draft | 2,891–2,942 s | 41.4–41.8 | 1.5 | 0 |
| | errors only | 70 s | 1.00 | 62.2 | 0 |
| | guard, no probe | 133–134 s | 1.91 | 32.5 | 0 |
| | guard + probe | 72–75 s | 1.03–1.07 | 58.1–60.6 | 0 |
| CDN, early shift | first draft | 3,185–3,277 s | 42.5–43.4 | 1.4–1.5 | 0 |
| | errors only | 75–76 s | 1.00 | 62.2–62.3 | 0 |
| | guard, no probe | 243–282 s | 3.25–3.73 | 16.6–19.1 | 0 |
| | guard + probe | 80–84 s | 1.06–1.11 | 56.1–58.6 | 0 |
| Small origin | first draft | 65–66 s | 1.00 | 62.7–63.1 | 137 |
| | errors only | 65–66 s | 1.00 | 62.7 | 137 |
| | guard, no probe | 65–66 s | 1.00 | 36.9–39.7 | 49–59 |
| | guard + probe | 66–67 s | 1.01–1.02 | 32.3–34.1 | 37–42 |

The first draft gave the same results with baseline windows of the last 300, 1,000 or 3,000 samples, or all of them: 2,898–2,917 s on the late-shift CDN site, and 137 ms of queueing on the small origin (seed 1).

**A.12 Response-time spread of real sites** (`bench/ttfb_spread.py`)

Same-host pages linked from each start page, requested one after another over one reused connection with curl, so the only load from us is one request at a time. Time to first byte excludes connection set-up; 200 responses only. Two runs, on 2026-10-19 and 2026-10-20.

| Site | Pages | Run 1: p10 / median / p90 (ms) | Median ÷ p10 | Run 2: p10 / median / p90 (ms) | Median ÷ p10 |
|---|---|---|---|---|---|
| www.python.org | 35 | 9.0 / 100.4 / 119.8 | 11.1 | 9.1 / 10.6 / 23.0 | 1.17 |
| docs.python.org | 40 | 9.7 / 102.1 / 114.8 | 10.6 | 8.5 / 101.0 / 112.2 | 11.9 |
| developer.mozilla.org | 40 | 35.6 / 37.7 / 94.5 | 1.06 | 9.4 / 11.7 / 40.7 | 1.24 |
| www.theguardian.com | 25 | 13.9 / 49.8 / 141.9 | 3.58 | 10.5 / 50.3 / 277.9 | 4.80 |
| en.wikipedia.org | 35 | 35.6 / 50.5 / 467.2 | 1.42 | 35.6 / 37.3 / 391.5 | 1.05 |

The first draft of §5.3 read any ratio above 2 as congestion. On www.python.org the ratio fell from 11.1 to 1.17 between runs, most likely because the first run filled the CDN's cache with the pages it requested: the same pages changed cost with no change in load.

**A.13 Start-URL redirects**

`curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' https://HOST/`, run on 2026-10-19 and again on 2026-10-20 with the same results:

| Host | Response |
|---|---|
| python.org | 301 → `https://www.python.org/` |
| theguardian.com | 301 → `https://www.theguardian.com/` |
| wikipedia.org | 301 → `https://www.wikipedia.org/` |
| mozilla.org | 301 → `https://www.mozilla.org/` |
| bbc.co.uk | 301 → `https://www.bbc.co.uk/` |
| github.com | 200 |
| docs.python.org | 302 → `https://docs.python.org/3/` (same host) |
| developer.mozilla.org | 302 → `https://developer.mozilla.org/en-US/` (same host) |

5 of the 6 bare domains redirect to `www`.
