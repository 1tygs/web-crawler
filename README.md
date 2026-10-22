# web-crawler

[![CI](https://github.com/1tygs/web-crawler/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/1tygs/web-crawler/actions/workflows/ci.yml)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A fast, accurate and polite web crawler for the command line. Give it a URL and it crawls that host, printing each page followed by the links on it.

## Install

```bash
uv tool install git+https://github.com/1tygs/web-crawler
```

You need [uv](https://docs.astral.sh/uv/) on macOS or Linux; it fetches Python 3.14 if you don't have it. Or skip installing: `uv run crawl` works from a clone, and so does [Docker](#docker).

## Usage

```console
$ crawl https://quotes.toscrape.com/ > links.txt
378 pages in 4.9 s (77.8 pages/s), 7,746 links found, 478 unique in-scope URLs
statuses: 200 (378), 308 (100)
time to first byte: p50 132.5 ms, p95 376.6 ms, p99 1.18 s
page bodies: 447.8 kB received, 1.9 MB decompressed
not printed: redirect to a URL already claimed (50)
busiest second: 132 pages
concurrency limit: 8 to 64, 38 at the end
peak RSS: 59.4 MiB, parser processes aside

$ head -4 links.txt
https://quotes.toscrape.com/
  https://quotes.toscrape.com/
  https://quotes.toscrape.com/login
  https://quotes.toscrape.com/author/Albert-Einstein
```

Pages stream to stdout as they finish, each followed by its links and a blank line. A progress line and the summary go to stderr.

| Option | What it does |
|---|---|
| `--max-depth N` | Only pages within N links of the start URL |
| `--max-pages N` | Stop after N pages |
| `--all-urls` | Print every URL on a page, images and scripts too, not just links |
| `--format jsonl` | One JSON object per page: `url`, `status`, `depth`, `links` |
| `--include-errors` | With jsonl, a record for each URL that gave no page too, saying why |
| `-o FILE` | Write to FILE rather than stdout |
| `--max-concurrency N` | At most N requests at once (default 64); the crawl adapts below it |
| `--max-rps R` | At most R requests a second |
| `-v`, `-vv`, `-q` | Log more, or only errors |

`crawl --help` lists every option.

## What to expect

- **One host.** Links to other hosts, subdomains included, are printed but not followed. If the start URL redirects to add or remove `www.`, the crawl moves with it.
- **Fast.** Up to 64 requests in flight, with pages parsed in a pool of processes: about 1,100 pages/s on an Apple M5 against a local server answering in 50 ms ([measured](DESIGN.md#appendix-a-measurements)). The server sets the pace, not the crawler.
- **Polite.** It identifies itself, obeys robots.txt and its `Crawl-delay`, adds requests while responses stay fast, and halves them at signs of distress such as a 429 or a timeout.
- **Accurate.** Each page is fetched once, and links are resolved as a browser resolves them: charset, `<base href>`, redirects and all.
- **Resilient.** It retries failures with backoff, honours `Retry-After`, and pauses while the host is down or blocking it, giving up after 5 minutes.
- **Bounded.** Crawler traps and links to files that are rarely HTML are printed but not followed, and each page is capped at 32 MiB. The summary counts what was skipped.
- **Interruptible.** Ctrl-C stops cleanly: only whole records reach stdout, and the summary still prints.
- **No JavaScript.** Links that a page creates only when its scripts run are missed ([limitations](DESIGN.md#13-limitations-risks-and-what-to-revisit)).

Exit codes: `0` finished, `1` internal error, `2` usage error, `3` couldn't start, `4` gave up on the host, `130` Ctrl-C, `141` stdout closed (as by `| head`), `143` SIGTERM.

## Docker

```bash
docker build -t web-crawler .
docker run --rm web-crawler https://quotes.toscrape.com/ > links.txt
```

For the progress line too, run it with `-it` and write to a folder you mount:

```bash
docker run --rm -it -v "$PWD:/work" -w /work --user "$(id -u):$(id -g)" \
  web-crawler -o links.txt https://quotes.toscrape.com/
```

## Development

```bash
make check                 # ruff, mypy --strict, tests with 100% branch coverage
uv run pre-commit install  # optional: ruff and mypy before each commit
```

CI runs the same checks on every pull request, the tests on both Linux and macOS, and builds the Docker image and crawls a test site with it. Each push to main also runs a 50,000-page benchmark crawl with performance gates.

## Further reading

- [DESIGN.md](DESIGN.md): how it works and why, with the measurements behind each choice
- [ADR-001](docs/adr/001-concurrency-control.md): how it finds the pace a server can take
- [The implementation plan](docs/implementation-plan.md): how v1 was built, step by step
- [bench/](bench/): the scripts that reproduce every measurement, and how to run them

## How this was built

The brief asks which tools were used, AI included, and how the work was checked.

- **Sources.** Primary ones, cited in DESIGN.md: RFC 9309 (robots.txt), RFC 6797 (HSTS) and RFC 3986 (URI equivalence); the WHATWG URL, HTML and Encoding standards; the CPython 3.14 documentation; and, for adaptive concurrency, TCP congestion control, Netflix's concurrency-limits and BBR.
- **AI.** [Claude Code](https://claude.com/claude-code) with Claude Opus 5.5, in the Claude desktop app, one session per stage:
  - *Design:* from the brief, through the `system-design` skill of Anthropic's engineering plugin. It benchmarked candidate HTML parsers, URL libraries, HTTP clients and robots.txt matchers on the development machine, an Apple M5, chose from the results, and wrote DESIGN.md.
  - *Review:* checked the design against the brief, tested it in simulation (DESIGN.md A.9–A.13) and revised it, adding [ADR-001](docs/adr/001-concurrency-control.md).
  - *Plan:* checked each library's behaviour at its pinned version, and wrote the [implementation plan](docs/implementation-plan.md).
  - *Build:* six pull requests, each with its tests and merged only once CI passed: [#2](https://github.com/1tygs/web-crawler/pull/2), [#4](https://github.com/1tygs/web-crawler/pull/4), [#5](https://github.com/1tygs/web-crawler/pull/5), [#7](https://github.com/1tygs/web-crawler/pull/7), [#8](https://github.com/1tygs/web-crawler/pull/8), [#9](https://github.com/1tygs/web-crawler/pull/9).
  - *Docs and setup:* short sessions wrote the README and set up the repository and licence.
- **Editor.** Cursor, for reviewing diffs and making small changes by hand, without AI.
- **Checking.** Claims were tested, not taken on trust: every measurement in DESIGN.md comes from a script in `bench/` or a simulation. The simulations rejected the first draft's concurrency law and `--max-depth` rules, and caught a bug in the review's own first fix; re-running a real-site measurement changed one of its numbers.

## License

[MIT](LICENSE)
