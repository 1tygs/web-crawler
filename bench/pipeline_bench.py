"""Appendix A.2/A.3: the parse pipeline's cost per page, and the coordinator's with and without
parser processes.

Usage: uv run --group bench python bench/pipeline_bench.py FIXTURES_DIR

Both run the crawler's own code. A.2 times web_crawler.parse.parse_page, which a parser process
runs for each page: decompress, decode, parse, extract, resolve, normalize, render. A.3 sends
gzipped pages through the parse gateway, web_crawler.pool, as the crawl's coordinator does:
submit, receive, dedupe the links, write the record.

FIXTURES_DIR holds saved pages, NAME.html, and if it's there the headers each was served with,
NAME.headers (curl -D), for its charset. A page is parsed as https://www.NAME.org/some/dir/page.
"""
import asyncio, gzip, os, pathlib, resource, statistics, sys, time
import brotli
import uvloop
from compression import zstd

from web_crawler.content import parse_content_type
from web_crawler.parse import parse_page
from web_crawler.pool import InlineParser, ProcessParser
from web_crawler.types import ParseJob, WorkerContext

MAX_BYTES = 32 * 1024 * 1024  # --max-page-bytes' default
ROUNDS, PER_ROUND = 7, 50


def load_pages(fixtures):
    """(name, url, page, charset, scope prefixes) for each saved page."""
    pages = []
    for path in sorted(pathlib.Path(fixtures).glob("*.html")):
        headers = path.with_suffix(".headers")
        content_type = None
        if headers.exists():
            for line in headers.read_text("latin-1").splitlines():
                if line.lower().startswith("content-type:"):
                    content_type = line.split(":", 1)[1].strip()
        host = f"www.{path.stem}.org"
        scope = (f"https://{host}/", f"http://{host}/")
        pages.append((path.stem, f"https://{host}/some/dir/page", path.read_bytes(),
                      parse_content_type(content_type)[1], scope))
    return pages


def encodings(page):
    return {"gzip": gzip.compress(page, 6), "br": brotli.compress(page, quality=5),
            "zstd": zstd.compress(page, 3), None: page}


def per_page(pages):
    print(f"A.2: parse_page per page, ms, the median of {ROUNDS} rounds of {PER_ROUND}")
    print(f"  {'page':<10} {'KB':>6} {'links':>6} {'gzip':>7} {'br':>7} {'zstd':>7} {'none':>7}")
    for name, url, page, charset, scope in pages:
        ctx = WorkerContext(scope, MAX_BYTES)
        times = []
        for encoding, body in encodings(page).items():
            job = ParseJob(url, body, charset, False, encoding)
            result = parse_page(job, ctx)
            rounds = []
            for _ in range(ROUNDS):
                started = time.perf_counter()
                for _ in range(PER_ROUND):
                    parse_page(job, ctx)
                rounds.append((time.perf_counter() - started) / PER_ROUND)
            times.append(statistics.median(rounds) * 1e3)
        print(f"  {name:<10} {len(page) // 1000:>6} {result.links_found:>6} "
              + " ".join(f"{t:7.2f}" for t in times))


def cpu():
    usage = resource.getrusage(resource.RUSAGE_SELF)  # this process alone, not its workers
    return usage.ru_utime + usage.ru_stime


async def coordinator(pages, workers, total):
    """Pages/s and this process's CPU per page, sending total gzipped pages, round robin."""
    ctx = WorkerContext(tuple(prefix for *_, scope in pages for prefix in scope), MAX_BYTES)
    jobs = [ParseJob(url, gzip.compress(page, 6), charset, False, "gzip")
            for _, url, page, charset, _ in pages]
    parser = ProcessParser(ctx, workers) if workers else InlineParser(ctx)
    if workers:  # start the workers first
        await asyncio.gather(*(parser.parse(jobs[0]) for _ in range(workers * 2)))
    seen = set()
    sink = open(os.devnull, "wb", buffering=1 << 20)
    room = asyncio.Semaphore(max(4, workers * 4))  # the parses in flight, as pipeline slots allow

    async def one(i):
        async with room:
            result = await parser.parse(jobs[i % len(jobs)])
        for key, _ in result.links:  # admission's dedupe
            if key not in seen:
                seen.add(key)
        sink.write(result.record)

    cpu_before, started = cpu(), time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(total)))
    elapsed, used = time.perf_counter() - started, cpu() - cpu_before
    await parser.close()
    sink.close()
    return total / elapsed, used / total * 1e6


if __name__ == "__main__":
    pages = load_pages(sys.argv[1])
    per_page(pages)
    print("\nA.3: the coordinator, gzipped pages, round robin: pages/s and its own CPU per page")
    for workers in (0, 1, 2, 4, 8):
        rate, us = uvloop.run(coordinator(pages, workers, 4000 if workers else 2000))
        print(f"  {workers} parser processes  {rate:8.0f} pages/s   coordinator CPU {us:7.1f} µs/page")
