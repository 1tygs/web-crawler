# web-crawler v1: implementation plan

**Status:** proposed · **Date:** 2026-10-20 · **Implements:** [DESIGN.md](../DESIGN.md) §15 (v1) · **Lands as:** five stacked PRs, one per DESIGN §15 step

"DESIGN §n" refers to DESIGN.md; a bare "§n" refers to this plan.

## Context

[DESIGN.md](../DESIGN.md) (reviewed, with [ADR-001](adr/001-concurrency-control.md)) specifies a fast, accurate, polite single-host crawler; nothing is implemented yet and the README still says "design stage". This plan turns the design into a build. It fixes the module boundaries and contracts, the data structures and algorithms each module owns, the tests that prove each one, and the order the code lands in.

DESIGN.md stays the source of truth for *what* and *why*; this document fixes *how*. Planning against the real libraries found a few things the design doesn't say. They are listed in §9 so that the PR implementing each one also updates DESIGN.md.

**v1 scope** is DESIGN §15's five steps: walking skeleton, accuracy, speed, robustness, limits and output. Designed but deferred until after v1: HSTS upgrade, `--strip-params`, `--respect-nofollow`, `--sitemaps`, `--stats-json`, `--no-cookies`, HTTP/2 transport.

---

## 1. Engineering rules for every PR

- **A pure core and an async shell.** Modules that *decide* do no I/O and don't use asyncio: `urls`, `content`, `encodings`, `parse`, `frontier`, `limiter`, `retry`, the robots policy and `stats`. Modules that *act* wire them to the world: `transport`, `fetcher`, `pool`, `output`, `engine` and `cli`. `tests/unit/test_architecture.py` parses each core module's imports with `ast` and fails if one imports asyncio, aiohttp, threading, socket or a shell module. That keeps DESIGN §4.1 true after the first refactor, and it's what lets DESIGN §14's multi-host front ends reuse the core.
- **One composition root.** Only `engine.crawl()` builds concrete objects. Everything else receives its collaborators (transport, clock, random source, sink), so tests swap in fakes without monkeypatching.
- **Types and lint.** Code passes `mypy --strict` and ruff (lint and format), with no `Any` in public signatures. Every runtime dependency except brotli ships `py.typed`; brotli gets a short stub in `typings/brotli.pyi`.
- **Hot paths stay small.** For each link, the coordinator does one set lookup and one dict lookup, and never parses a URL (keys and the scope check come from the worker). For each page, there is one pickle in each direction. Nothing on the loop blocks.
- **Small commits, each with its tests.** Every PR is green in CI. DESIGN.md and the README change in the same PR as the code that changes them.
- **Measure before asserting.** Performance claims come from `bench/`. Bench scripts import production code (`web_crawler.parse`, `web_crawler.limiter`) rather than prototypes, so their numbers describe what ships.

## 2. Library behaviour verified for this plan

Checked on 2026-10-20 against the versions pinned in Appendix A (Python 3.14.6, aiohttp 3.14.5, yarl 1.25.1, selectolax 1.0.1, ada-url 4.0.0, protego 0.7.0, brotli 1.2.0), in the design session's scratch venv:

| Finding | Consequence for the code |
|---|---|
| yarl, which aiohttp uses for URLs, rewrites them by default: `?b=%2f` becomes `?b=/`, `%7E` becomes `~`, `/a/%2E%2E/b` resolves to `/b`, and `%zz` becomes `%25zz` | The transport passes `yarl.URL(url, encoded=True)`, so the request line is the WHATWG string byte for byte. An integration test asserts that the server sees exactly the printed URL |
| aiohttp closes a connection, rather than pooling it, when the response body wasn't read to EOF (`ResponseHandler.should_close`) | DESIGN §6.2's "reuse only if read to the end" holds when the response context exits through `release()` |
| aiohttp's default header limits are 8,190-byte lines and fields and 128 headers | Raised to 64 KiB, 64 KiB and 256 (`max_line_size`, `max_field_size`, `max_headers`), so a huge `Set-Cookie` or CSP header doesn't fail a page |
| aiohttp exposes connection set-up timing only through `TraceConfig` (`on_connection_create_end`, `on_connection_queued_end`) | Time to first byte is measured from connection-ready (A.12's method). The trace's overhead is gated in PR 3 |
| aiohttp connects to the port in its resolver's result | A test resolver maps `example.test:80` and `:443` to local ports, so default-port scope, http↔https and apex→`www` run end to end without root |
| selectolax parses `bytes` as UTF-8, and `scripting=False` (`<noscript>` parsed as markup) is the default. `encoding=True` detection falls back to UTF-8, not windows-1252 | UTF-8 pages go in as bytes; other charsets are decoded by our code (§5.4). lexbor's own detection stays off |
| `<a href>` (no value) yields `None`; `href=""` yields `''` | `None` is treated as `""`: both resolve to the document's URL |
| `a[href]` matches by local name, so it also matches SVG `<a xlink:href>`, whose `attributes` has only `xlink:href` | Extraction reads `href`, then falls back to `xlink:href` |
| `template_fragments()` returns top-level templates only, including inert ones and bogus `shadowrootmode` values | Recursion is explicit, and only `shadowrootmode` of `open` or `closed` (ASCII case-insensitive) counts as a shadow root |
| `meta[http-equiv="refresh" i]` works in lexbor's selector engine | Meta refresh is filtered in C, not in Python |
| ada-url's `join_url(base, "")` drops the base's fragment. Trailing-dot hosts stay distinct (`example.com.`) | No special case for empty hrefs. A trailing-dot host is out of scope (pinned in a test) |
| brotli's `output_buffer_limit` is a soft cap: 1 MiB asked, 2.06 MiB returned. zlib's and zstd's `max_length` are exact | The worker slices brotli output to the cap after decoding |
| Raw deflate (no zlib header) fails with `wbits` 15 and 47 | `deflate` checks for a zlib header and falls back to raw deflate, as browsers do |
| Python's codec names aren't WHATWG labels: `latin1` and `ascii` mean iso8859-1 and ascii to Python but windows-1252 to a browser; `x-mac-cyrillic`, `koi8-ru` and `unicode11utf8` don't exist in Python | A WHATWG label table (`encodings.py`) maps labels to Python codecs |
| Python 3.14 has `ProcessPoolExecutor.kill_workers()`, `os.process_cpu_count()` and `asyncio.run(loop_factory=)`. `PYTHON_CPU_COUNT` and `-X cpu_count` override `process_cpu_count()`; isolated mode (`-I`) ignores the variable | The second Ctrl-C calls `kill_workers()`. uvloop comes in through `loop_factory`. The cgroup cap applies only when no override is set (§5.12) |
| Protego has `can_fetch`, `crawl_delay`, `request_rate` and `sitemaps`, and matches percent-encoding equivalents (`/~j` against `/%7Ej`) | Paths need no normalization before `can_fetch` |
| Found in PR 1: aiohttp's `trust_env=True` reads `~/.netrc` and looks up the proxy on a thread for every request, 162.5 µs of CPU per request against 26.9 µs (DESIGN A.4), and sends `~/.netrc` credentials to the crawled host | `trust_env=False`. The transport reads `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` once |
| Found in PR 1: aiohttp 3.14 deprecates `proxy_auth`, but the `proxy_headers` it recommends instead reach the proxy only on the CONNECT that tunnels `https://` | Proxy credentials go on the request itself for `http://`, and in `proxy_headers` for `https://` |

## 3. Repository layout and tooling

```text
web-crawler/
├── pyproject.toml            # uv_build backend; console script crawl = web_crawler.cli:main
├── uv.lock
├── Makefile                  # sync, fmt, lint, typecheck, test, cov, bench, demo, check
├── Dockerfile                # PR 5
├── .pre-commit-config.yaml   # ruff check + format; mypy as a local hook via `uv run`, so it sees project deps
├── .github/workflows/ci.yml
├── .github/dependabot.yml    # uv, github-actions, docker; weekly
├── typings/brotli.pyi
├── src/web_crawler/
│   ├── __init__.py, __main__.py
│   ├── cli.py                # shell: argparse → Config; logging; exit codes
│   ├── config.py             # frozen Config + validation
│   ├── types.py              # shared frozen contracts (DESIGN §6.2)
│   ├── urls.py               # scope, dedupe key, start-URL normalization, trap and extension rules
│   ├── content.py            # Content-Type, capped decompression, HTML sniffing, charset detection
│   ├── encodings.py          # WHATWG label → Python codec table (data)
│   ├── parse.py              # parse_page(), pure; runs in the workers
│   ├── frontier.py           # Admission + Frontier: seen set, depth queues and window, retry heap, accounting
│   ├── limiter.py            # ADR-001 law, pause and rate gate, circuit breaker, 403 guard
│   ├── retry.py              # DESIGN §5.4 classification and backoff, as pure functions
│   ├── robots.py             # RobotsPolicy (pure) + RFC 9309 fetching (async)
│   ├── stats.py              # counters, TTFB histogram, progress line, summary
│   ├── transport.py          # Transport/Response protocols + the aiohttp implementation
│   ├── fetcher.py            # redirects, triage, size caps
│   ├── pool.py               # parse gateway
│   ├── output.py             # Sink protocol + writer-thread sink
│   └── engine.py             # crawl(): startup, dispatcher, tasks, termination, signals
├── tests/
│   ├── unit/   integration/   fixtures/
│   └── support/              # test site server, test resolver + TLS, fake transport, fake clock, bench loaders
└── bench/                    # existing scripts, now importing production code; + e2e.py (PR 3)
```

This differs from DESIGN §12 only by splitting existing boxes:
- `types.py` holds the contracts that core and shell share.
- `content.py` and `encodings.py` are separate because the fetcher (sniffing), robots (decoding) and the workers all need them. Keeping them out of `parse.py` stops the coordinator importing lexbor just for that.
- `retry.py` turns the DESIGN §5.4 table into one pure function.

**pyproject.toml**
- `requires-python = ">=3.14"`.
- Runtime dependencies have floors at the Appendix A versions and caps below the next major: aiohttp, uvloop (`sys_platform != 'win32'`), selectolax, ada-url, protego, brotli, orjson. Exact versions live in `uv.lock`.
- `dev` group: pytest, pytest-asyncio (`asyncio_mode = "auto"`), hypothesis (a derandomized CI profile), coverage, mypy, ruff, trustme (TLS certificates for the test servers), pre-commit.
- `bench` group: lxml, beautifulsoup4, httpx, curl_cffi, niquests, yarl. This replaces `bench/requirements.txt` in PR 3, when the benches start importing the package; the README's bench commands change to `uv run --group bench …`.
- Ruff: line length 100; rule sets E, F, W, I, B, UP, SIM, RUF, ASYNC, PERF, PT and PL, with test-only exemptions for magic values and argument counts.
- mypy: `strict = true`, `mypy_path = "typings"`.
- Coverage: branch coverage, `concurrency = ["thread", "multiprocessing"]`. `fail_under` starts at what PR 1 reaches and rises to 90 by PR 5.

**CI (`ci.yml`)** runs on every PR and every push to main:
- `lint`: `ruff check` and `ruff format --check`.
- `typecheck`: `mypy --strict src tests`.
- `test`: a matrix of {ubuntu-latest, macos-latest} on Python 3.14 through `astral-sh/setup-uv`, running `uv sync --locked` then pytest with coverage.
- `ci-ok`: `needs` all of the above. This is the single required check to add to ruleset 25443421 once PR 1 merges; its name doesn't change when the matrix does.

Actions are pinned by commit SHA and Dependabot keeps them current. The 50k-page end-to-end benchmark (§6) is a separate, non-required job on pushes to main that uploads its JSON results. Target runtime: unit tests under 10 s, the whole suite under 2 minutes per OS.

**Makefile** wraps `uv run`, so nobody manages a venv by hand. `make check` runs lint, typecheck and test.

---

## 4. Reuse from `bench/`

| Existing code | Becomes |
|---|---|
| `bench/limiter_sim.py` `GuardProbeLaw`, `run()` | `limiter.ConcurrencyLaw` (production). The simulator stays in the bench script; its "guard+probe" row instantiates the production class, and `tests/unit/test_limiter.py` loads the script (a `tests/support` loader using `importlib.util.spec_from_file_location`) to run its scenarios. That's one simulator, and evidence and code can't drift apart (ADR-001 action items 1–2) |
| `bench/depth_sim.py` `make_site()`, `bfs()` | Random site graphs and ground truth for the frontier's property tests and the integration test sites, through the same loader |
| `bench/pipeline_bench.py` `parse_page`, `decompress`, `_attrs_links`, `dedupe_key` | The prototypes for `parse.py`, `content.py` and `urls.py`, rewritten with charset detection, output caps, spec-exact meta refresh and the shadow-root mode check. The bench then imports production `parse_page` |
| `bench/pool_check.py` `ignore_sigint_and_unblock`, the masked `submit()` | `pool.py`'s initializer and submit path. Its checks become tests (SIGINT while workers start, one crash among busy workers) |
| `bench/server.py` (pre-fork aiohttp) | The base of `bench/e2e.py`'s synthetic-site server |
| `bench/client_bench.py` | Gains an `aiohttp+uvloop+raw+trace` client to price the TTFB trace |

---

## 5. Module design

### 5.1 `types.py`: contracts (DESIGN §6.2, extended)

```python
@dataclass(frozen=True, slots=True)
class CrawlItem:
    url: str                              # display URL: requested and printed
    key: str                              # dedupe key; the same object as url when url has no '%'
    depth: int
    attempt: int = 0                      # error retries used (--retries, default 3)
    throttled: int = 0                    # 429/503 retries used (own budget of 5)
    redirected_from: tuple[str, ...] = () # hops already followed: retries resume mid-chain; JSONL reports them

@dataclass(frozen=True, slots=True)
class ParseJob:                           # as DESIGN §6.2
    url: str; depth: int; status: int; content_encoding: str | None; http_charset: str | None
    body: bytes; redirected_from: tuple[str, ...]; truncated: bool

class ParseResult(NamedTuple):
    record: bytes                         # rendered output, written verbatim (b"" on a decode error)
    links: list[tuple[str, str]]          # in-scope (key, url), unique by key, in document order
    links_found: int                      # links printed on the page, for stats
    decoded_bytes: int                    # for stats: wire vs decoded
    error: str | None                     # e.g. "bad_encoding": deterministic, so final

@dataclass(frozen=True, slots=True)
class WorkerContext:                      # sent once, through the pool initializer
    scope_prefixes: tuple[str, ...]; fmt: Literal["text", "jsonl"]; all_urls: bool; max_bytes: int
```

### 5.2 `config.py` and `cli.py`

- `Config` is a frozen dataclass mirroring DESIGN §6.1, with its defaults. The User-Agent is `web-crawler/1.0 (+https://github.com/1tygs/web-crawler)`, and its product token `web-crawler` selects the robots.txt group. Validation runs in `__post_init__`: ranges, and `--include-errors` requiring `--format jsonl` (§9). Programmatic callers (tests, DESIGN §14's library) get the same checks as the CLI. The trap thresholds (2,048 characters, 3 repeats, 1,000 query variants) are `Config` fields with defaults rather than CLI flags, which makes them configurable without widening DESIGN §6.1.
- `cli.main()`:
  - argparse → `Config`, exiting 2 on a usage error;
  - size arguments accept binary K, M and G suffixes, and `--parse-workers` takes `auto` or an integer ≥ 0;
  - sets up logging: `-q` prints errors only (no progress line or summary), `-v` adds failures, sampled rejections and limiter decisions, and `-vv` adds every request;
  - calls `asyncio.run(engine.crawl(cfg, sink), loop_factory=uvloop.new_event_loop)`, with the default loop on Windows;
  - exits with `CrawlReport.exit_code`.
- **Exit codes:**
  - `0`: complete.
  - `1`: internal error. An unexpected exception in one page's processing is logged with its traceback, counted, and the crawl continues; the exit code is 1 if any occurred.
  - `2`: usage error.
  - `3`: couldn't start.
  - `4`: aborted.
  - `130`, `143`: SIGINT, SIGTERM.
  - `141`: stdout closed (§9).

### 5.3 `urls.py` (pure)

- `normalize_start(raw) -> (url, scheme_was_implicit)`: prepends `https://` when `raw` has no `scheme://`, so `localhost:8000` isn't parsed as the scheme `localhost:`. It then parses with ada-url, requires http(s) and strips the fragment. A start URL with credentials (`user:pass@`) is a usage error: authenticated areas are out of scope (DESIGN §2.5), and its serialized form would fail its own scope check.
- `Scope.for_url(url)`: built from ada-url's hostname and port.
  - With a default port: prefixes `("https://h/", "http://h/")` and both origins.
  - With an explicit port: its own scheme only.
  - `contains(url)` is `url.startswith(prefixes)`, and IPv6 literals keep their brackets.
  - `origins` lists the robots.txt origins.
- `is_www_variant(a, b)`: `a == "www." + b or b == "www." + a`.
- `dedupe_key(url)`: the RFC 3986 §6.2.2 equivalences on top of WHATWG. Escapes of unreserved bytes are decoded and other escapes uppercased. The regex runs only when `"%" in url`; otherwise the same object is returned.
- `strip_fragment(url)`; `trap_rule(url, cfg) -> str | None` (URL length, or a path segment that occurs more than 3 times); `has_skipped_extension(url)` (the last path segment's suffix is in a frozenset grouped by DESIGN §5.1's categories: images, audio and video, archives and binaries, fonts, PDF, office documents, CSS, JS).
- Tests:
  - table tests for `www`, subdomains, IDN, case, trailing dot, ports, userinfo, and http vs https;
  - a pinned subset of WPT `urltestdata.json` (BSD-3-Clause, credited in the fixture folder);
  - hypothesis properties: `dedupe_key` is idempotent and never merges URLs that differ by more than the case of an escape or an unreserved escape.

### 5.4 `content.py` and `encodings.py` (pure)

- `parse_content_type(value) -> (mime, charset | None)`: WHATWG "extract a MIME type", with a lowercase essence and quoted parameters handled.
- `decode_body(body, content_encoding, cap) -> (bytes, truncated)`, raising `ContentDecodeError`:
  - `identity`;
  - `gzip`/`x-gzip` (wbits 47, looping over concatenated members);
  - `deflate` (zlib header check, else raw deflate);
  - `br` (`output_buffer_limit=cap`, then sliced);
  - `zstd` (`max_length`);
  - stacked encodings such as `gzip, br`, decoded in reverse with the cap at every stage.

  A stream that ends cleanly but early returns its partial output with `truncated=True`, which is what a browser renders. Corrupt data raises.
- `sniff_html(prefix)`: the WHATWG MIME-sniffing HTML patterns for an unknown type over the first 512 decoded bytes: leading whitespace skipped, then `<!DOCTYPE HTML`, `<HTML`, `<HEAD`, `<SCRIPT`, … or `<!--`, each followed by a tag-terminating byte.
- `pick_charset(body, http_charset) -> codec`, in this order:
  1. BOM.
  2. HTTP label.
  3. `prescan(body[:1024])`, the HTML standard's prescan algorithm (UTF-16 → UTF-8, x-user-defined → windows-1252).
  4. UTF-8, if `body.isascii()` or the body decodes as UTF-8.
  5. Otherwise windows-1252.
- `encodings.py` holds all ~220 labels of the Encoding Standard, mapped through its encodings to Python codecs:
  - gbk → gb18030, big5 → big5hkscs, shift_jis → cp932, euc-kr → cp949, x-mac-cyrillic → mac_cyrillic;
  - `replacement` → no content.

  A test checks it against a vendored copy of the standard's `encodings.json` (CC BY 4.0, credited in the fixture folder), so every label resolves.

### 5.5 `parse.py`: the worker

`parse_page(job, ctx) -> ParseResult` takes the `WorkerContext` as an argument, so it stays pure and the inline path needs no global (a refinement made in PR 1). In the pool, `init_worker(ctx)` stores the context in a module global, sets SIGINT and SIGTERM to ignored, then unblocks them (DESIGN §4.3), and each job runs a module-level wrapper that calls `parse_page(job, ctx)`, so the context still crosses to a worker only once. `parse_page`:

1. `decode_body(job.body, job.content_encoding, ctx.max_bytes)`. On `ContentDecodeError`, return `ParseResult(b"", [], 0, 0, "bad_encoding")`.
2. `pick_charset`. UTF-8 goes to lexbor as bytes, with no copy; other charsets go as `bytes.decode(codec, "replace")`.
3. `LexborHTMLParser(html)`, with scripting off (the default).
4. **Base URL:** the first `base[href]`, resolved against `job.url`. It's ignored if resolution fails or gives `data:` or `javascript:` (HTML standard).
5. **Extract** in document order with one selector list.
   - Navigational: `a[href], area[href], iframe[src], frame[src], link[href][rel], meta[http-equiv="refresh" i][content]`.
   - `--all-urls` adds every URL-bearing attribute: `src` and `srcset` on img and source (through the HTML srcset parser), `src` on script, embed, video, audio, track and input, video `poster`, object `data`, form `action`, button `formaction`, `cite` on blockquote, q, del and ins, link `imagesrcset`, body `background`.
   - `link` keeps `canonical`, `alternate`, `next` and `prev`, and drops `alternate stylesheet`.
   - An `a` matched by local name falls back to `xlink:href`, and a valueless attribute is `""`.
   - Meta refresh `content` follows the standard's declarative-refresh steps; a refresh with no URL isn't a link.
   - Then recurse into `template_fragments()` whose host has `shadowrootmode` of open or closed, and into their nested shadow roots. Inert templates are skipped.
6. Resolve each value with `ada_url.join_url(base, value)`. A `ValueError` skips that value (counted).
7. Keep http(s) only (every scheme with `--all-urls`), strip the fragment, and dedupe within the page by display URL, keeping the first occurrence.
8. In-scope links become `(dedupe_key(u), u)`, unique by key. The canonical URL is kept for JSONL.
9. **Render.** Text is `url\n` plus `  link\n` per link, then a blank line. JSONL goes through `orjson.dumps(…, option=OPT_APPEND_NEWLINE)` with `url`, `status`, `depth` and `links`, plus `redirected_from`, `truncated` and `canonical` when present.

Golden fixtures (`tests/fixtures/parse/*.html` with expected JSON) cover:
- each base form; entities, whitespace and `\`;
- shadow DOM vs inert and bogus-mode templates, and nested shadow roots;
- noscript in `<head>`; every meta-refresh variant; frames; SVG xlink; empty and valueless hrefs;
- `--all-urls` with srcset;
- charset via HTTP, meta, BOM, and undeclared windows-1252;
- truncated and malformed HTML; every content-encoding; bombs.

### 5.6 `frontier.py`: `Admission` and `Frontier` (pure)

**State:**
- `seen: set[str]` and `queues: list[deque[CrawlItem]]`, indexed by depth;
- `queued: dict[str, CrawlItem]`, holding only items now in a queue, so they can be found again to lower their depth;
- a `retries` heap of `(ready_at, seq, item)`;
- `incomplete: list[int]` per depth (queued, in flight, retrying or held by the 403 guard) and `outstanding`;
- `_low`, the shallowest incomplete depth, and `_q_low`, the shallowest queue that may be non-empty;
- `query_variants: dict[str, int]`;
- rejection counters with the first 3 examples per rule.

**Methods:**
- **`Admission.admit(links, depth)`**, for each `(key, url)`, cheapest check first:
  1. If `key in seen`, maybe lower it: when `queued.get(key)` is deeper, create a new frozen item at `depth`, set `queued[key]` to it, append it to its queue, and move the incomplete count. The old deque entry goes stale and is skipped on pop by an identity check.
  2. Trap rule.
  3. Query-variant cap.
  4. Extension filter.
  5. `robots.allowed(url)`.
  6. `--max-depth`. A depth rejection isn't claimed, so a shorter path can still admit the URL.
  7. Otherwise claim it (`seen.add`) and enqueue it.

  Every other rejection is claimed, so it's checked only once.
- **`Admission.admit_hop(item, url, key) -> HopDecision`**:
  - the key is in the item's own chain → `FOLLOW` (cookie-gated redirects that come back, §9);
  - claimed elsewhere → `CLAIMED`;
  - robots or a trap rule → `REJECTED(rule)`;
  - otherwise claim it → `FOLLOW`.
- **`Frontier.pop(now)`** moves due retries to the *front* of their depth queue. It returns the head of the shallowest non-empty queue, skipping stale entries (`queued.get(item.key) is not item`). Under `--max-depth`, it returns `None` when that depth is more than `_low + 1`; that is the two-level window of DESIGN §5.1.
- **`next_retry_at()`, `schedule_retry()`, `finish(item)`** (decrements `incomplete` and `outstanding`, advances `_low`) and **`drop_queued()`** (for `--max-pages` and abort).

**Invariant:** the shallowest incomplete depth never decreases. A page at depth d only adds or lowers items to depth d+1, which is greater than `_low`. So `_low` only moves forward, in O(1) amortized time.

**Tests:** hypothesis drives `admit`, `pop` and `finish` over generated link graphs and completion orders, and checks that:
- each key is dispatched at most once;
- `outstanding == 0` exactly when nothing is queued, in flight or retrying;
- under `--max-depth`, the set dispatched equals the BFS ground truth. That's A.10's property, now on production code.

### 5.7 `limiter.py` (pure, with an injected clock)

- **`ConcurrencyLaw`** is the bench's `GuardProbeLaw` made production-ready.
  - `limit` is an integer ≥ 1. It starts at min(8, ceiling); the ceiling is `--max-concurrency`.
  - Methods: `on_sample(ttfb, issued_at, now)` and `on_congestion(issued_at, now)`.
  - A congestion signal cuts only if its request was issued after the last cut: one cut per congestion event, the exact form of "once per RTT" (§9). A cut ends slow start, cancels a probe and discards the partial round.
  - Both methods return a `Decision` (grow, hold, slow-start exit, probe start, probe verdict, cut), so the engine logs it at `-v` and tests assert on it.
  - Samples come from responses with status below 500, excluding 429.
- **`PauseGate`**: the Retry-After pause (the later of the current and new pause, capped at 300 s), plus the rate ceiling. Each request start sets the next allowed start to now + 1/rate, with rate = min(`--max-rps`, 1/Crawl-delay). That spaces requests evenly, with no bursts.
- **`CircuitBreaker`**: opens after 20 consecutive connection-level failures (DNS, connect, TLS, connect timeout). Health probes back off from 1 s to 60 s, and any HTTP response closes it. If it stays open for 5 minutes, the crawl aborts with exit 4. While it's open, connection failures don't use up the items' retry budgets.
- **`BlockGuard`** (the 403 rule) counts consecutive 403s after at least one page. A 403'd item is held while the run is under 20: it stays outstanding, so `--max-depth` stays exact and a URL that's requeued never gets two records (§9). A run is settled in one of three ways:
  - by a non-403 response: the held items become final;
  - by tripping at 20: pause, probe the start URL with backoff, then requeue the held items once on recovery, or abort after 5 minutes;
  - when nothing else can make progress: the held items become final.
- **`parse_retry_after(value, now_wall)`**: seconds or an HTTP date.

**Tests:**
- The four A.11 scenarios, run on `ConcurrencyLaw` through the bench simulator. Where there's spare capacity, crawl time stays within 1.15× the errors-only law's; on the small origin, queueing stays under 0.5× the errors-only law's.
- A new scenario: an origin that answers 429 above 20 requests in flight. The limit settles near 20 with one cut per burst. The ADR's table never exercised the cut path.
- Retry-After pauses; the rate gate's spacing; breaker and guard state machines on a fake clock.

### 5.8 `retry.py` (pure)

- `classify(status_or_error_kind) -> Class`: the DESIGN §5.4 table.
- `next_attempt(item, cls, retry_after, cfg, rng) -> (CrawlItem, delay) | None`. Backoff uses full jitter, `uniform(0, min(30, 0.5 · 2^attempt))`. 429 and 503 draw on their own budget of 5. When the budget runs out, the result is `None`, meaning final.

### 5.9 `robots.py`

- **`RobotsPolicy`** (pure) holds one Protego object per origin prefix. It provides `allowed(url)` and `crawl_delay`, plus `ALLOW_ALL` and `DISALLOW_ALL` for the status classes. Groups match on the User-Agent's product token.
- **`fetch_robots(transport, origin)`** (async) follows at least 5 redirects, across hosts if needed (RFC 9309).
  - 2xx: the first 500 KiB after decoding (a capped `decode_body`), read as UTF-8 with replacement.
  - 4xx other than 429: allow everything.
  - 429, 5xx or network errors: 3 tries with backoff, then disallow everything.
  - More than 5 redirects: unavailable, so allow everything.

  If the start origin ends up disallowing everything, the crawl exits 3 with a hint about `--ignore-robots`. Every origin is refetched in the background every 24 hours; if a refetch fails, the cached policy stays.

### 5.10 `transport.py`

- **Protocols** as in DESIGN §6.2. `read(limit) -> (bytes, more)` can be called repeatedly to stream in steps. `more` is exact because the implementation reads `limit + 1` bytes and keeps the extra one buffered.
- **`AiohttpTransport` configuration:**
  - `TCPConnector(limit = limit_per_host = max_concurrency + 4, ttl_dns_cache=300, keepalive_timeout=30)`;
  - `ClientSession(auto_decompress=False, trust_env=False, cookie_jar=CookieJar(unsafe=True), timeout=ClientTimeout(total=--timeout, sock_connect=10, sock_read=20), …)`, with the raised header limits and default headers for User-Agent, Accept and `Accept-Encoding: gzip, deflate, br, zstd`. `unsafe=True` lets IP-address hosts set cookies, as browsers do. Proxies come from the environment, read once (§2);
  - `ssl=False` under `--insecure`;
  - `resolver` and `ssl_context` arguments exist for tests only.
- **Error mapping.** aiohttp's exceptions become `TransportError(kind)`, so nothing above this module imports aiohttp. The kinds are DNS, CONNECT, TLS, CONNECT_TIMEOUT, TIMEOUT, RESET, PAYLOAD (cut off mid-body), PROTOCOL and INVALID_URL.
- **TTFB** is header time minus the later of request start and connection-ready time; the trace sets connection-ready.

### 5.11 `fetcher.py`

`fetch(item, hops: HopPolicy) -> FetchOutcome` returns one of `Page(ParseJob)`, `NotPage(status, mime)`, `Claimed`, `Retry(item_at_hop, cls, retry_after)` or `Final(reason, status, chain)`.

It loops over at most 10 hops. Each iteration opens the request, feeds the limiter, breaker and guard, then calls the pure `triage(status, headers)`:
- **301, 302, 303, 307, 308 with `Location`:** resolve with ada-url, strip the fragment, `await hops.admit_hop(…)`, drain up to 64 KiB, and continue.
- **2xx HTML:** `read(max_page_bytes)`.
- **2xx with no Content-Type:** read the first 16 KiB, sniff the decoded prefix, and keep reading only if it's HTML.
- **Any other 2xx:** drain if `Content-Length ≤ 64 KiB`, otherwise close.
- **429 or 503:** `Retry` (throttle class, with Retry-After).
- **408, 500, 502, 504:** `Retry` (error class).
- **Any other status ≥ 400:** `Final`.

`HopPolicy` is async so that the start item's policy can fetch robots.txt for a new `www.` origin mid-chain. The normal policy never awaits.

### 5.12 `pool.py`: parse gateway

- **Worker count.** `auto` is `max(0, n − 1)`, where n is `os.process_cpu_count()`. Unless `PYTHON_CPU_COUNT` or `-X cpu_count` is set, n is also capped by the cgroup quota: v2 `cpu.max`, read up the process's cgroup path, or v1 `cfs_quota_us/cfs_period_us`. A count of 0 means parse inline.
- **Start method.** forkserver on Linux with `set_forkserver_preload(["web_crawler.parse"])`, so workers start with lexbor and ada-url already imported; spawn elsewhere.
- **Submit.** `submit()` runs under `pthread_sigmask(SIG_BLOCK, {SIGINT, SIGTERM})`, restored in `finally`, and is awaited through `asyncio.wrap_future`.
- **Crash isolation (DESIGN §5.4).**
  - The gateway keeps a pool generation number. A `BrokenProcessPool` rebuilds the pool once per generation and resubmits the job, without using up retries.
  - A job that breaks the pool twice runs alone in a one-worker isolation pool. Suspects go one at a time while normal traffic continues.
  - If it breaks that pool too, it raises `ParseCrashed`, a final parse error for that page only.
- **`close(kill=False)`.** `shutdown(wait=True)`, or `kill_workers()` when `kill=True`.
- **When the pool is created.** Once the scope is fixed after the start URL's redirects, because the initializer carries the scope prefixes (§9).

### 5.13 `output.py`

- **`Sink` protocol:** `start(on_drained)`, `write(record)`, `backlogged` and `close()`. This is the seam DESIGN §14.1 names. `start` hands the sink the callback that wakes the dispatcher, since the CLI builds the sink before the event loop runs.
- **`ThreadedSink(binary_stream)`** puts records on a `SimpleQueue` for a writer thread:
  - each batch of queued records goes out in one `write()`, and the thread flushes at most every 100 ms and whenever it's idle;
  - the backlog is two single-writer counters, bytes enqueued on the loop thread and bytes written on the writer thread, so no lock is needed;
  - `backlogged` turns on above 64 MiB, and the thread calls `loop.call_soon_threadsafe(on_drained)` once the backlog falls below 32 MiB;
  - on `BrokenPipeError`, it `dup2`s devnull over the fd, calls `on_closed`, and discards the rest.
- **`MemorySink`** is for tests.

### 5.14 `stats.py` (pure)

- **Counters:** pages, links found, unique in-scope URLs (`len(seen)`), statuses, errors by class, rejections by rule with examples, wire and decoded bytes, limiter range, peak pages/s over 1-second windows, and peak RSS (normalized for Linux KiB vs macOS bytes).
- **TTFB** goes into a log-bucketed histogram (1% buckets, fixed memory) that gives p50, p95 and p99.
- **Output:** `progress_line()` and `summary()`. The progress line is refreshed four times a second, and only when stderr is a terminal; when stdout is a terminal too, it suggests redirecting to a file. A logging handler clears and redraws it, so `-v` log lines don't tear it.

### 5.15 `engine.py`

`async def crawl(cfg, sink, *, transport=None, clock=time.monotonic, rng=None) -> CrawlReport`

**Startup**
1. `normalize_start`, then `Scope`, then open the transport.
2. Fetch robots.txt for the scope's origins in parallel, unless `--ignore-robots`. If the start URL is disallowed, exit 3.
3. Fetch the start item under a `StartHopPolicy`:
   - same-host hops are admitted normally;
   - a host change that only adds or removes `www.` (unless `--strict-host`) re-scopes, says so on stderr, and fetches the new origins' robots.txt before the hop;
   - any other host change exits 3 and prints the URL to use.

   The start item's retries run inline, since nothing else is queued yet. A start URL whose scheme was implicit and fails at connect or TLS is retried once as `http://`. A start outcome that isn't a page exits 3 with its reason.
4. The scope is now fixed. Create the pool, and send the start page through the normal parse path.

**Dispatch.** `_pump()` is synchronous. It runs after every completion, permit release, timer and sink drain:

```text
while not stopping:
    if sink.backlogged or in_pipeline >= slots or requests_in_flight >= limiter.limit: return
    if max_pages and pages_done + in_pipeline >= max_pages: return
    if (wait := gates.delay(now)) > 0: wake_at(now + wait); return   # Retry-After, rate, breaker, guard
    item = frontier.pop(now)
    if item is None:
        if in_pipeline == 0 and frontier.next_retry_at() is None:
            block_guard.settle()             # nothing else can end the run of 403s
            if frontier has work again: continue
        wake_at(frontier.next_retry_at())    # when there is one
        return
    gates.take(now); start(item)
```

- `wake_at` keeps a single `TimerHandle` at the earliest deadline.
- Pipeline slots default to `max_concurrency + 4 × workers`.
- A task runs `fetch`, which holds a limiter permit. The permit is released as soon as `fetch` returns. Then, for a `Page`: `gateway.parse` → `sink.write(record)` → `frontier.admit(links, depth + 1)` → `finish(item)` → `_pump()`.
- Children are admitted before their parent finishes, so `outstanding == 0` means exactly "done" (DESIGN §4.2 step 8), unless the block guard is mid-trip. Under `--max-pages`, the crawl ends when `pages_done == N`; by then nothing can be in flight.

**Shutdown**
- **First SIGINT or SIGTERM:**
  1. Stop dispatching.
  2. Cancel the tasks still fetching; they're tracked apart from the tasks parsing.
  3. Let submitted parses finish and print.
  4. Close the pool, then the transport (with a graceful TLS close), then the sink.
  5. Print the summary and exit 130 or 143.
- **Second SIGINT:** `kill_workers()`, then `os._exit(130)`.
- **Abort (breaker or block guard):** the first path, with exit 4.
- **Stdout closed:** the first path, with exit 141.

---

## 6. Test plan

| Layer | Where | What it proves |
|---|---|---|
| Unit, pure | `tests/unit/` | The tables, properties and golden fixtures in §5.3–§5.9, the TTFB histogram's accuracy, and the import rule in `test_architecture.py` |
| Fetcher | `tests/unit/test_fetcher.py`, with a scripted fake transport | The triage table; hop admission; the 10-hop limit vs a cookie gate that redirects back; drain vs close; the truncation flag; the sniff path; error mapping |
| Integration | `tests/integration/` using `tests/support/site.py`: an in-process aiohttp server over http and https (trustme), a test resolver for `*.test:80/443`, and a request log | Output equals ground truth on hand-built sites and on seeded random graphs from `depth_sim.make_site`, with and without `--max-depth` and `--max-pages`. Also: no URL requested twice; the request line equals the printed URL byte for byte; robots rules; apex→`www` followed; another host → exit 3; scheme-less `localhost:PORT`; http/https variants collapse; a subdomain printed but never requested |
| Fault injection | `tests/integration/test_faults.py` | A reset mid-body; truncated gzip; 429/503 with Retry-After; a run of 403s that recovers, and one that doesn't (exit 4); an unreachable host (breaker, exit 4); `kill -9` of a busy worker (no page lost); a crasher page (only it fails); SIGINT mid-crawl and while workers start (whole records only, summary printed, 130); stdout closed (`\| head`, 141, no traceback); a decompression bomb |
| Performance | `bench/e2e.py`; non-required CI job on main | 50k synthetic pages, ~120 links each, 50 ms injected latency, served by a pre-fork server: pages/s, coordinator CPU per page (from `wait4` rusage), peak RSS, written to JSON. Gates are calibrated in PR 3 and enforced from PR 5 |

---

## 7. Delivery: five stacked PRs

Branches `v1/1-skeleton` through `v1/5-limits-output`, each based on the one before. When a PR merges (merge commit, branch deleted), GitHub retargets the next one to main. Each PR is a few small commits, each with its tests. PR descriptions become the merge-commit message, so they're written as permanent history.

### PR 1: Walking skeleton
1. Scaffolding: pyproject, uv.lock, tool config, Makefile, pre-commit, brotli stub, `python -m web_crawler --version`.
2. CI workflow with `ci-ok`; Dependabot.
3. `types`, `config` and `cli` (parsing, exit 2) with tests.
4. `urls`: `normalize_start`, `Scope`, `strip_fragment`.
5. `transport`: aiohttp with encoded URLs, error mapping, and auto-decompression **on** for now. Plus `tests/support/site.py`.
6. Inline `parse_page` (lexbor + ada-url, http(s) only, fragments stripped, in-page dedupe, scope), and `output` (`ThreadedSink`, text).
7. Minimal `frontier`, `fetcher` (manual redirects with a scope check; HTML or not by Content-Type), `engine` (fixed concurrency, termination) and `stats`; the first integration tests; the README status.

**Done when** `crawl http://127.0.0.1:PORT/` on test sites prints exactly the BFS ground truth, and CI is green on Linux and macOS. **After merge:** add `ci-ok` as a required check on ruleset 25443421, with your go-ahead.

### PR 2: Accuracy
1. `dedupe_key`, scope edge-case tables, the WPT URL subset.
2. `content.pick_charset` with prescan, and `encodings.py` tested against `encodings.json`.
3. Complete `parse.py`: base href, shadow DOM, meta refresh, link rel, SVG xlink, empty href, `--all-urls` with srcset; golden fixtures.
4. Per-hop claims, the cookie-gate return, records for redirect loops and the hop limit.
5. `robots.py`, the robots check in admission, `--ignore-robots`.
6. Start-URL handling: the implicit-scheme fallback to http, the `www.` switch, `--strict-host`, the exit-3 cases. Plus the test-resolver and TLS harness, and default-port integration tests.

**Done when** every DESIGN §2.4 accuracy rule has a test, and the integration suite shows each URL requested at most once.

### PR 3: Speed
1. `content.decode_body` and `sniff_html`. The transport switches to `auto_decompress=False`, and `ParseJob` carries the encoding.
2. `pool.py`: worker count with cgroups, start methods and forkserver preload, masked submits, crash isolation. Tests: kill -9, a crasher page, SIGINT while workers start.
3. `limiter.ConcurrencyLaw`, with simulator-backed tests and the 429 scenario. `bench/limiter_sim.py` now uses the production class.
4. Engine: limiter permits, pipeline slots, `PauseGate` (`--max-rps`, Crawl-delay), and the `--max-concurrency` and `--parse-workers` options.
5. TTFB from connection-ready through the trace, priced with `client_bench.py`'s new `+trace` client.
6. `bench/e2e.py`; `pipeline_bench.py` on production `parse_page`; the `bench` dependency group; DESIGN.md and ADR-001 updates.

**Gates:**
- The trace adds at most 5 µs per request. If it costs more, TTFB falls back to marking samples from new connections, which then count only when a round has no other samples.
- The full worker pipeline stays within 10% of A.2 on the same pages.

### PR 4: Robustness
1. `retry.py` and the retry heap, with backoff.
2. The 429/503 budget and the Retry-After pause.
3. `CircuitBreaker`, the health probe, exit 4.
4. `BlockGuard` (the 403 rule).
5. Trap rules, the query-variant cap, the extension filter and `--no-skip-extensions`.
6. The wire size cap and `truncated` flag; drain or close for non-HTML; `--timeout`, `--retries`, `--max-page-bytes`.
7. Signals and shutdown (130/143, the second Ctrl-C), stdout closed (141); the fault-injection suite.

**Done when** every row of DESIGN §5.4's table has a test, and the fault suite passes on both OSes.

### PR 5: Limits and output
1. `--max-pages` dispatch gating.
2. The `--max-depth` window and depth lowering, with the property tests against BFS.
3. JSONL (`redirected_from`, `truncated`, `canonical`), `--include-errors` records (errors and non-HTML), `-o`.
4. The TTFB histogram, the progress line, the full summary; decision logs at `-v` and per-request logs at `-vv`.
5. Dockerfile.
6. The e2e CI job with its gates enforced; README usage and status; the remaining §9 updates to DESIGN.md; ADR-001 action item 3.

**Done when** the §8 checks pass.

---

## 8. Verification

- **Every PR:** `make check` locally (ruff, `mypy --strict`, pytest with coverage), and CI green on Linux and macOS.
- **Before PR 5 merges, end to end:**
  1. `uv run crawl --format jsonl https://www.python.org/ --max-pages 200 -v`, and the same against docs.python.org. Expect no tracebacks, the limiter's decisions logged, a sensible summary, and every printed URL valid WHATWG. Spot-check 20 pages' links against a browser.
  2. `bench/e2e.py` with 50k pages at 50 ms: record pages/s, coordinator CPU per page (expected within DESIGN §1's 140–230 µs) and peak RSS (expected under 300 MB).
  3. Rerun `bench/pipeline_bench.py` and `bench/limiter_sim.py` on production code, and update Appendix A if the numbers moved.
  4. Interrupt a crawl piped into `| head` and one writing to a file: only whole records, and the exit codes in §5.2.

## 9. Design refinements to fold into DESIGN.md

Each lands in the PR that implements it:

1. **TTFB from connection-ready,** not from request start, as in A.12's method; it keeps handshake time out of the base round. PR 3.
2. **One cut per congestion event:** a cut happens only for congestion on requests issued after the previous cut, the exact form of "once per RTT". A cut also discards the partial round. PR 3.
3. **Cookie-gated redirects:** a redirect back to a URL already in the same chain is followed. Real loops end at the 10-hop limit and are reported as loops. PR 2.
4. **The pool starts once the scope is fixed,** after the start URL's redirects, because its initializer carries the scope prefixes. The cost is one worker start before the first parse. PR 3.
5. **403s are held until their run is settled,** which keeps `--max-depth` exact and gives a requeued URL only one record. PR 4.
6. **A closed stdout exits 141** (128 + SIGPIPE), consistent with 130 and 143; DESIGN §6.1 is silent on it. PR 4.
7. **`ParseResult` gains `links_found`, `decoded_bytes` and `error`.** PRs 1 and 3.
8. **A compressed stream that ends cleanly but early** is parsed as far as it got and flagged `truncated`, as a browser renders it; only corrupt data is a final error. PR 3.
9. **`--include-errors` requires `--format jsonl`.** PR 5.
10. **A.11 gains a 429-ing origin,** covering the cut path that ADR-001's table doesn't. PR 3.

## 10. Risks and open questions

- **Trace overhead** is unmeasured. PR 3 measures it, and the fallback is already defined.
- **Windows** has no CI: no uvloop, and signal handling is best effort.
- **windows-1252 edge bytes.** WHATWG's windows-1252 maps 5 bytes that Python's cp1252 leaves undefined. Decoding with `replace` affects only hrefs that contain those raw bytes.
- **Legacy-encoded query strings** behave as described in DESIGN §13.
- **Performance gates** depend on how fast CI runners are. They're calibrated in PR 3 and enforced relative to that baseline from PR 5.
