# web-crawler

A fast, accurate and polite web crawler for the command line, written in Python. Give it a start URL and it crawls that host, printing each page's URL followed by every link found on the page.

> **Status: design stage.** [DESIGN.md](DESIGN.md) is the proposed design; the crawler itself isn't implemented yet. The measurements behind the design can be reproduced with the scripts in [`bench/`](bench/).

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

- Only the start URL's exact host is crawled. Links to other domains and to subdomains are printed but never fetched.
- Results stream to stdout as pages complete; progress and a final summary go to stderr.
- `--format jsonl` gives machine-readable output; `--max-pages` and `--max-depth` bound the crawl.

Every option and exit code is listed in [DESIGN.md §6.1](DESIGN.md#61-command-line).

## Design in brief

- **Fast.** One asyncio (uvloop) coordinator owns all crawl state and does only cheap work. A process pool parses response bodies while they're still compressed. An adaptive concurrency limit (AIMD plus a latency signal) runs close to what the server can sustain, and backs off on 429/503 and `Retry-After`.
- **Accurate.** Links are resolved as a browser resolves them: HTML5 parsing (selectolax/lexbor), WHATWG URLs (ada-url), `<base href>`, the final URL after redirects, and declarative shadow DOM. Each page is fetched exactly once (redirect-aware dedupe against an exact set), transient failures are retried, and every skipped URL is counted.
- **Polite.** robots.txt per RFC 9309, `Crawl-delay`, and an honest User-Agent.

Measured on an Apple M5, the full parse pipeline costs 0.4–2.8 ms per real page and eight parser processes handle about 2,900 pages/s, so the target server, not the crawler, sets the pace.

Requires Python 3.14 or later. No JavaScript runs, so links created only by client-side rendering aren't seen; see [limitations](DESIGN.md#13-limitations-risks-and-what-to-revisit).

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

| Command | Measures | Appendix |
|---|---|---|
| `python bench/parse_bench.py bench/fixtures` | Link extraction: lexbor vs lxml, html.parser and BeautifulSoup | A.1 |
| `python bench/pipeline_bench.py bench/fixtures` | Full parse pipeline per page; coordinator throughput with 0–8 parser processes | A.2, A.3 |
| `python bench/client_bench.py CLIENT 64 20000` | HTTP client CPU per request | A.4 |
| `python bench/url_bench.py bench/fixtures` | URL resolution: `urljoin` vs yarl vs ada-url | A.5 |
| `python bench/robots_bench.py bench/fixtures` | robots.txt matching: Protego vs `urllib.robotparser` | A.6 |
| `python bench/memory_bench.py` | Memory per URL in the seen set and frontier | A.7 |

- `client_bench.py` needs `python bench/server.py PAGE.html` running in another shell; it serves that page on 127.0.0.1:8899. `CLIENT` is one of `aiohttp`, `aiohttp+uvloop`, `aiohttp+uvloop+raw` (no decompression), `curl_cffi`, `niquests` or `httpx`.
- `robots_bench.py` expects `wikipedia.html` and `wikipedia.robots.txt` from en.wikipedia.org, and `guardian.html` and `guardian.robots.txt` from www.theguardian.com.
