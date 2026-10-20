# web-crawler

A fast, accurate and polite web crawler for the command line, written in Python. Give it a start URL and it crawls that host, printing each page's URL followed by every link found on the page.

> **Status: design stage.** [DESIGN.md](DESIGN.md) is the proposed design, and [docs/implementation-plan.md](docs/implementation-plan.md) is the plan for building v1; the crawler itself isn't implemented yet. The measurements behind the design can be reproduced with the scripts in [`bench/`](bench/).

## Planned usage

```bash
crawl https://example.com/
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
- Results stream to stdout as pages complete; progress and a final summary go to stderr.
- `--format jsonl` gives machine-readable output; `--max-pages` and `--max-depth` bound the crawl.

Every option and exit code is listed in [DESIGN.md §6.1](DESIGN.md#61-command-line).

## Design in brief

- **Fast.** One asyncio (uvloop) coordinator owns all crawl state and does only cheap work. A process pool parses response bodies while they're still compressed. An adaptive concurrency limit runs close to what the server can sustain: it halves on 429/503 and timeouts, honours `Retry-After`, and stops growing when a probe confirms the server is queueing ([ADR-001](docs/adr/001-concurrency-control.md)).
- **Accurate.** Links are resolved as a browser resolves them: HTML5 parsing (selectolax/lexbor), WHATWG URLs (ada-url), `<base href>`, the final URL after redirects, and declarative shadow DOM. Each page is fetched exactly once (redirect-aware dedupe against an exact set), transient failures are retried, and every skipped URL is counted.
- **Polite.** robots.txt per RFC 9309, `Crawl-delay`, and an honest User-Agent.

Measured on an Apple M5, the full parse pipeline costs 0.4–2.8 ms per real page and eight parser processes handle about 2,900 pages/s, so the target server, not the crawler, sets the pace.

Requires Python 3.14 or later. No JavaScript runs, so links created only by client-side rendering aren't seen; see [limitations](DESIGN.md#13-limitations-risks-and-what-to-revisit).

## How this was built

The brief asks which tools were used, including AI, and how the work was checked.

- **Learning the domain.** Primary sources, as cited in DESIGN.md: RFC 9309 (robots.txt), RFC 6797 (HSTS) and RFC 3986 (URI equivalence); the WHATWG URL, HTML and Encoding standards; the CPython 3.14 documentation; and, for adaptive concurrency, TCP congestion control, Netflix's concurrency-limits and BBR.
- **Measuring instead of assuming.** Performance and behaviour claims in DESIGN.md come from the scripts in `bench/`, run on the development machine (an Apple M5), or from simulations where a real server can't be controlled. Appendix A has the methods and results.
- **Editor.** Cursor for diff review and small manual changes without AI.
- **AI tools.**
  - **Claude Code**, Anthropic's coding agent, in the Claude desktop app with Claude Opus 5.5. The first draft of the design came from a session that started from the brief through the `system-design` skill of Anthropic's engineering plugin. It installed candidate libraries in a scratch virtual environment, then wrote and ran the benchmarks for HTML parsers, URL resolution, HTTP clients, the parse pipeline, robots.txt matching and memory per URL on the development machine. It picked each technology from those results and wrote DESIGN.md with the scripts moved into `bench/`. Separate short sessions wrote the README and set up the public repository and licence. In a review session it reviewed the design against this brief, wrote and ran the checks and simulations in Appendix A.9–A.13, and applied the resulting changes to DESIGN.md, including [ADR-001](docs/adr/001-concurrency-control.md).
- **Checking AI output.** Claims were tested rather than taken on trust, and the scripts that test them are kept in `bench/`. The review's simulations rejected the first draft's limiter law and its `--max-depth` rules, and caught a bug in the review's own first proposal for the limiter; re-running a real-site measurement changed one of its numbers (A.12).

## Development

You need [uv](https://docs.astral.sh/uv/) 0.12.15 or later; it installs Python 3.14 itself if necessary. The Makefile runs everything through uv, so there's no virtual environment to manage.

```bash
make check                    # ruff, mypy --strict and the tests with coverage
make test PYTEST_ARGS="-x"    # just the tests
uv run pre-commit install     # optional: ruff and mypy before each commit
```

CI runs the same targets on Linux and macOS for every pull request.

## Benchmarks

The scripts in `bench/` produce the measurements in [DESIGN.md Appendix A](DESIGN.md#appendix-a-measurements). They need [uv](https://docs.astral.sh/uv/), Python 3.14, and macOS or Linux.

```bash
uv venv --python 3.14
source .venv/bin/activate
uv pip install -r bench/requirements.txt
```

Most scripts read a folder of saved HTML pages. These aren't committed because they're third-party content, so save your own:

```bash
mkdir -p bench/fixtures
curl --compressed -o bench/fixtures/python.html https://www.python.org/
```


| Command                                         | Measures                                                                               | Appendix |
| ----------------------------------------------- | -------------------------------------------------------------------------------------- | -------- |
| `python bench/parse_bench.py bench/fixtures`    | Link extraction: lexbor vs lxml, html.parser and BeautifulSoup                         | A.1      |
| `python bench/pipeline_bench.py bench/fixtures` | Full parse pipeline per page; coordinator throughput with 0–8 parser processes         | A.2, A.3 |
| `python bench/client_bench.py CLIENT 64 20000`  | HTTP client CPU per request                                                            | A.4      |
| `python bench/url_bench.py bench/fixtures`      | URL resolution: `urljoin` vs yarl vs ada-url                                           | A.5      |
| `python bench/robots_bench.py bench/fixtures`   | robots.txt matching: Protego vs `urllib.robotparser`                                   | A.6      |
| `python bench/memory_bench.py`                  | Memory per URL in the seen set and frontier                                            | A.7      |
| `python bench/pool_check.py MODE`               | Process pool: on-demand workers, what one crash takes down, Ctrl-C while workers start | A.9      |
| `python bench/depth_sim.py`                     | `--max-depth` rules against breadth-first-search ground truth, in simulation           | A.10     |
| `python bench/limiter_sim.py`                   | Concurrency laws on simulated servers                                                  | A.11     |
| `python bench/ttfb_spread.py URL`               | How widely a real site's response times spread                                         | A.12     |


- `client_bench.py` needs `python bench/server.py PAGE.html` running in another shell; it serves that page on 127.0.0.1:8899. `CLIENT` is one of `aiohttp`, `aiohttp+uvloop`, `aiohttp+uvloop+raw` (no decompression), `curl_cffi`, `niquests` or `httpx`. Adding `+env` to an aiohttp client turns on `trust_env`.
- `robots_bench.py` expects `wikipedia.html` and `wikipedia.robots.txt` from en.wikipedia.org, and `guardian.html` and `guardian.robots.txt` from [www.theguardian.com](http://www.theguardian.com).
- `pool_check.py` takes a `MODE` of `ondemand`, `crash` or `sigint`, and `--method spawn` or `--method forkserver`. It and the two simulations use only the standard library.
- `ttfb_spread.py` needs curl, and sends about 40 requests, one at a time, to the site you give it.

