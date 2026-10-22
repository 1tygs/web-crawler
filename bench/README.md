# Benchmarks

The scripts in `bench/` produce the measurements in [DESIGN.md Appendix A](../DESIGN.md#appendix-a-measurements). They need [uv](https://docs.astral.sh/uv/), and macOS or Linux. They run in the project's environment with uv's `bench` dependency group, which adds the libraries the crawler was compared with:

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
