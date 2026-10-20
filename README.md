# web-crawler

[![CI](https://github.com/1tygs/web-crawler/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/1tygs/web-crawler/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/downloads/)
[![mypy: strict](https://img.shields.io/badge/mypy-strict-blue)](https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-strict)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A fast, accurate and polite web crawler for the command line, written in Python. Give it a start URL and it crawls that host, printing each page's URL followed by every link found on the page.

> **Status: v1 is complete: all five steps of [the v1 plan](docs/implementation-plan.md) are built.** `crawl URL` crawls the start URL's host breadth-first and prints each page's links as a browser resolves them: in the page's charset, against its `<base href>`, from declarative shadow DOM and meta refresh too, and with `--all-urls` every URL-bearing attribute. It keeps as many requests in flight as the server sustains, adapting to its response times and halving on signs of distress ([ADR-001](docs/adr/001-concurrency-control.md)), and parses pages in a pool of processes, decompressing them there. It obeys robots.txt (RFC 9309) and its `Crawl-delay`, fetches each page once however many links and redirects lead to it, and follows a start URL's redirect to its `www.` host. It tries failed requests again with backoff, honours `Retry-After`, pauses while the host can't be reached or seems to block it, bounds crawls that a site's links could make endless, and caps each page's size. `--max-pages` and `--max-depth` bound a crawl exactly, `--format jsonl` prints machine-readable records, a progress line and a summary say how it's going, and Ctrl-C stops it cleanly. [DESIGN.md](DESIGN.md) describes the design, and the measurements behind it can be reproduced with the scripts in [`bench/`](bench/).

## Usage

With [uv](https://docs.astral.sh/uv/) installed (it fetches Python 3.14 itself if needed):

```bash
git clone https://github.com/1tygs/web-crawler.git
cd web-crawler
uv run crawl https://example.com/
```

```text
https://example.com/
  https://example.com/about
  https://example.com/blog/
  https://github.com/example

https://example.com/about
  https://example.com/
```

- Only the start URL's exact host is crawled (after following a redirect that only adds or removes `www.`). Links to other domains and to subdomains are printed but never fetched.
- Results stream to stdout as pages complete, or to a file with `-o FILE`. stderr gets the rest: a progress line while the crawl runs, when stderr is a terminal and the records go elsewhere, and a summary at the end: pages, links, statuses, time to first byte, what wasn't printed or crawled and why, the busiest second, the concurrency limit's range and peak memory.
- `--format jsonl` prints a JSON object per page instead: its `url`, `status`, `depth` and `links`, and `redirected_from`, `truncated` and `canonical` when there's something to say. With `--include-errors`, every URL that ended without a page gets a record too, saying why in `error`: a 404, a connection that failed after its retries, a response that isn't HTML, a redirect off the host. `jq` and `sort` work on it, since output isn't sorted.

  ```text
  {"url":"https://example.com/blog/","status":200,"depth":1,"links":["https://example.com/","https://example.com/blog/post-1"],"redirected_from":["https://example.com/blog"]}
  {"url":"https://example.com/missing","status":404,"depth":2,"error":"HTTP 404"}
  ```
- `--max-pages N` stops once N pages are printed: exactly N records, or fewer if the site is smaller. A fetch starts only while pages printed plus fetches in flight is below N, so no page is fetched only to be thrown away.
- `--max-depth N` crawls exactly the pages within N links of the start URL, however the fetches interleave: a page is fetched only once no shorter path to it can turn up, and its JSONL `depth` is then exact. `--max-depth 0` crawls the start URL alone.
- `--max-concurrency N` caps the requests in flight, 64 by default; below it, the crawl finds what the server sustains. `--max-rps R` starts at most R requests a second, evenly spaced; a robots.txt `Crawl-delay` can lower that.
- `--parse-workers N` sets how many processes parse pages: `auto`, the default, is one fewer than the CPUs, within a container's CPU quota, and 0 parses in the crawler's own process.
- A request that gets no answer (it can't connect, times out or is cut off), or gets a 408, 500, 502 or 504, is tried again up to `--retries N` times (3 by default), after a random backoff that doubles each time, from up to 0.5 s to at most 30 s. A 429 or 503 is tried again up to 5 times whatever `--retries` says, and its `Retry-After` holds every request back for as long as it asks, up to 5 minutes. `--timeout S` is the most one request may take, 60 s by default.
- If 20 requests in a row can't reach the host, the crawl pauses and checks the start page, from 1 s to 60 s apart, until it answers; 20 403s in a row look like a firewall's block, and pause it the same way, until the start page is served again. Either way, the crawl gives up after 5 minutes, keeping what it printed, with exit code 4.
- Some links aren't crawled, though they're printed: URLs over 2,048 characters, paths that repeat a segment more than 3 times, more than 1,000 query strings for one path, and files that are almost never HTML, by their extension (images, media, archives, fonts, PDFs, office documents, CSS and JS; `--no-skip-extensions` crawls them too). The summary counts each, with examples.
- `--max-page-bytes N` caps each page, 32 MiB by default (`512K`, `32M` and `2G` work too), as sent and as decompressed: a page cut short is parsed as far as it got.
- Ctrl-C (SIGINT) or SIGTERM stops the crawl cleanly: no more fetches start, those in flight are cancelled, pages already being parsed are printed, only whole records reach stdout, and the summary still goes to stderr. The exit code is 130 for SIGINT and 143 for SIGTERM. A second Ctrl-C exits at once. When stdout's reader goes, as `| head` does once it has read enough, the crawl ends quietly with exit code 141.
- `-v` logs the URLs that fail, the first few URLs each rule keeps from being crawled, and the concurrency limit's decisions; `-vv` logs every request too. `-q` prints errors only.

`crawl --help` describes every option, and [DESIGN.md §6.1](DESIGN.md#61-command-line) lists them with the exit codes: 0 when the crawl completes, 1 for an internal error, 2 for a usage error, 3 when it can't start, 4 when it gives up on its host, 130, 141 and 143 as above.

### With Docker

The image runs the crawler without a local Python, as a user who isn't root:

```bash
docker build -t web-crawler .
docker run --rm web-crawler https://example.com/ > out.txt
docker run --rm web-crawler --format jsonl --max-depth 2 https://example.com/ > out.jsonl
```

With `-it`, Ctrl-C stops the crawl, but Docker sends stdout and stderr down one terminal, so the records show there and there's no progress line. For the progress line, have the crawler write the records to a file in a folder you mount, as you:

```bash
docker run --rm -it -v "$PWD:/work" -w /work --user "$(id -u):$(id -g)" web-crawler -o out.txt https://example.com/
```

## Design in brief

This describes the v1 design, all of it built.

- **Fast.** One asyncio (uvloop) coordinator owns all crawl state and does only cheap work. A process pool parses response bodies while they're still compressed. An adaptive concurrency limit runs close to what the server can sustain: it halves on 429/503 and timeouts, honours `Retry-After`, and stops growing when a probe confirms the server is queueing ([ADR-001](docs/adr/001-concurrency-control.md)).
- **Accurate.** Links are resolved as a browser resolves them: HTML5 parsing (selectolax/lexbor), WHATWG URLs (ada-url), `<base href>`, the final URL after redirects, and declarative shadow DOM. Each page is fetched exactly once (redirect-aware dedupe against an exact set), transient failures are retried, and every skipped URL is counted.
- **Polite.** robots.txt per RFC 9309, `Crawl-delay`, and an honest User-Agent.

Measured on an Apple M5, the crawler's parse pipeline costs 0.4–2.7 ms per real page and eight parser processes handle about 2,800 pages/s, so the target server, not the crawler, sets the pace.

Requires Python 3.14 or later. No JavaScript runs, so links created only by client-side rendering aren't seen; see [limitations](DESIGN.md#13-limitations-risks-and-what-to-revisit).

## How this was built

The brief asks which tools were used, including AI, and how the work was checked.

- **Learning the domain.** Primary sources, as cited in DESIGN.md: RFC 9309 (robots.txt), RFC 6797 (HSTS) and RFC 3986 (URI equivalence); the WHATWG URL, HTML and Encoding standards; the CPython 3.14 documentation; and, for adaptive concurrency, TCP congestion control, Netflix's concurrency-limits and BBR.
- **Measuring instead of assuming.** Performance and behaviour claims in DESIGN.md come from the scripts in `bench/`, run on the development machine (an Apple M5), or from simulations where a real server can't be controlled. Appendix A has the methods and results.
- **Editor.** Cursor for diff review and small manual changes without AI.
- **AI tools.**
  - **Claude Code**, Anthropic's coding agent, in the Claude desktop app with Claude Opus 5.5. The first draft of the design came from a session that started from the brief through the `system-design` skill of Anthropic's engineering plugin. It installed candidate libraries in a scratch virtual environment, then wrote and ran the benchmarks for HTML parsers, URL resolution, HTTP clients, the parse pipeline, robots.txt matching and memory per URL on the development machine. It picked each technology from those results and wrote DESIGN.md with the scripts moved into `bench/`. Separate short sessions wrote the README and set up the public repository and licence. In a review session it reviewed the design against this brief, wrote and ran the checks and simulations in Appendix A.9–A.13, and applied the resulting changes to DESIGN.md, including [ADR-001](docs/adr/001-concurrency-control.md).
  - **Implementation.** A planning session checked each library's behaviour against the pinned versions and wrote [docs/implementation-plan.md](docs/implementation-plan.md). Separate Claude Code sessions then built the steps from it, one pull request each ([#2](https://github.com/1tygs/web-crawler/pull/2), [#4](https://github.com/1tygs/web-crawler/pull/4), [#5](https://github.com/1tygs/web-crawler/pull/5), [#7](https://github.com/1tygs/web-crawler/pull/7), [#8](https://github.com/1tygs/web-crawler/pull/8), [#9](https://github.com/1tygs/web-crawler/pull/9)), each with its tests, and each merged only once CI passed: ruff, mypy --strict and the tests on Linux and macOS, and from the last, the Docker image too.
- **Checking AI output.** Claims were tested rather than taken on trust, and the scripts that test them are kept in `bench/`. The review's simulations rejected the first draft's limiter law and its `--max-depth` rules, and caught a bug in the review's own first proposal for the limiter; re-running a real-site measurement changed one of its numbers (A.12).

## Development

You need [uv](https://docs.astral.sh/uv/) 0.12.15 or later; it installs Python 3.14 itself if necessary. The Makefile runs everything through uv, so there's no virtual environment to manage.

```bash
make check                    # ruff, mypy --strict and the tests with coverage
make test PYTEST_ARGS="-x"    # just the tests
uv run pre-commit install     # optional: ruff and mypy before each commit
```

CI runs the same targets on Linux and macOS for every pull request, and builds the Docker image and crawls a small site with it. On each push to main, a separate job runs `bench/e2e.py`'s 50,000-page crawl and fails if pages/s, the coordinator's CPU per page or its peak memory crosses its gate ([DESIGN.md A.15](DESIGN.md#appendix-a-measurements)); it isn't a required check, as GitHub's runners vary.

## Benchmarks

The scripts in `bench/` produce the measurements in [DESIGN.md Appendix A](DESIGN.md#appendix-a-measurements). They need [uv](https://docs.astral.sh/uv/), and macOS or Linux. They run in the project's environment with uv's `bench` dependency group, which adds the libraries the crawler was compared with:

```bash
uv run --group bench python bench/limiter_sim.py
```

Most scripts read a folder of saved HTML pages. These aren't committed because they're third-party content, so save your own, with the headers each was served with, which give its charset. Appendix A used these five, fetched on 2026-10-20:

```bash
mkdir -p bench/fixtures
cd bench/fixtures
curl --compressed -D python.headers -o python.html https://www.python.org/
curl --compressed -D docs.headers -o docs.html https://docs.python.org/3/library/asyncio-eventloop.html
curl --compressed -D mdn.headers -o mdn.html https://developer.mozilla.org/en-US/docs/Web/HTML
curl --compressed -D wikipedia.headers -o wikipedia.html https://en.wikipedia.org/wiki/Web_crawler
curl --compressed -D guardian.headers -o guardian.html https://www.theguardian.com/international
```

Each command runs as `uv run --group bench python bench/SCRIPT …`:

| Script and arguments                | Measures                                                                                       | Appendix |
| ----------------------------------- | ---------------------------------------------------------------------------------------------- | -------- |
| `parse_bench.py bench/fixtures`     | Link extraction: lexbor vs lxml, html.parser and BeautifulSoup                                 | A.1      |
| `pipeline_bench.py bench/fixtures`  | The crawler's parse pipeline per page; the coordinator with 0–8 parser processes               | A.2, A.3 |
| `client_bench.py CLIENT 64 20000`   | HTTP client CPU per request                                                                    | A.4      |
| `url_bench.py bench/fixtures`       | URL resolution: `urljoin` vs yarl vs ada-url                                                   | A.5      |
| `robots_bench.py bench/fixtures`    | robots.txt matching: Protego vs `urllib.robotparser`                                           | A.6      |
| `memory_bench.py`                   | Memory per URL in the seen set and frontier                                                    | A.7      |
| `pool_check.py MODE`                | Process pool: on-demand workers, what one crash takes down, Ctrl-C while workers start         | A.9      |
| `depth_sim.py`                      | `--max-depth` rules against breadth-first-search ground truth, in simulation                   | A.10     |
| `limiter_sim.py`                    | Concurrency laws, the crawler's among them, on simulated servers                               | A.11     |
| `ttfb_spread.py URL`                | How widely a real site's response times spread                                                 | A.12     |
| `encoding_check.py INDEXES`         | The crawler's legacy decoders against the Encoding Standard's indexes                          | A.14     |
| `e2e.py`                            | A whole crawl of 50,000 synthetic pages at 50 ms: pages/s, coordinator CPU per page, peak RSS  | A.15     |
| `admission_bench.py`                | What admission's rules cost per URL a crawl hasn't seen                                        | A.16     |

- `client_bench.py` needs `uv run --group bench python bench/server.py PAGE.html` running in another shell; it serves that page, gzipped, on 127.0.0.1:8899. `CLIENT` is one of `aiohttp`, `aiohttp+uvloop`, `aiohttp+uvloop+raw` (no decompression), `curl_cffi`, `niquests` or `httpx`. Adding `+env` to an aiohttp client turns on `trust_env`; `+trace` times each response through aiohttp's `TraceConfig`, and `+ready` as the crawler's transport does.
- `robots_bench.py` expects `wikipedia.html` and `wikipedia.robots.txt` from en.wikipedia.org, and `guardian.html` and `guardian.robots.txt` from [www.theguardian.com](http://www.theguardian.com).
- `pool_check.py` takes a `MODE` of `ondemand`, `crash` or `sigint`, and `--method spawn` or `--method forkserver`; it uses only the standard library. `depth_sim.py` runs the crawler's own frontier beside the rules it simulates.
- `ttfb_spread.py` needs curl, and sends about 40 requests, one at a time, to the site you give it.
- `encoding_check.py`'s `INDEXES` is the Encoding Standard's [`indexes.json`](https://github.com/whatwg/encoding/blob/main/indexes.json).
- `e2e.py` starts its own server and runs the crawl in its own process; `--help` lists its settings, `--json FILE` keeps the results, and `--min-pages-per-second`, `--max-cpu-us-per-page` and `--max-peak-rss-mib` are the gates CI runs it with.
