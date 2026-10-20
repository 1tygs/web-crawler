"""The crawler end to end on a synthetic site: pages per second, the coordinator's CPU per page and
its peak memory (docs/implementation-plan.md §6).

Usage: uv run --group bench python bench/e2e.py [--pages 50000] [--latency 0.05]
           [--max-concurrency 64] [--parse-workers auto] [--server-processes 4] [--json FILE]

A pre-fork aiohttp server, in processes of its own, serves --pages pages at /p/0 to /p/N-1, each
after --latency seconds, gzipped: a page has about 70 KB of HTML with 120 links and some text,
like the average page DESIGN.md §7 assumes. Page n links to page n+1, so that every page can be
reached, and to 119 others picked at random, as on a real site, plus two other hosts. The crawl
runs in this process as the crawl command runs it, writing to /dev/null, so this process's own
CPU is the coordinator's: the parser processes' isn't counted.
"""
import argparse, asyncio, collections, gzip, json, logging, os, platform, random, re, resource
import signal, socket, statistics
import subprocess, sys, time

WORDS = ("crawler page link server host frontier parse queue robots request response latency "
         "limit worker process pool header body charset anchor resolve normalize scope depth").split()


def page_html(n, pages, paragraphs):
    rng = random.Random(n)
    links = ([n + 1] if n + 1 < pages else []) + rng.sample(range(pages), 119)
    items = "".join(f'<li><a href="/p/{target}">Page {target}</a></li>\n' for target in links)
    text = "".join(f"<p>{paragraphs[rng.randrange(len(paragraphs))]}</p>\n" for _ in range(18))
    return (f"<!doctype html>\n<html lang=en><head><meta charset=utf-8><title>Page {n}</title>"
            f'<link rel=canonical href="/p/{n}"></head>\n<body><nav><ul>\n{items}</ul></nav>\n'
            f'<main>{text}</main><footer><a href="https://example.com/">elsewhere</a> '
            f'<a href="https://example.org/about">about</a></footer></body></html>\n').encode()


def serve(sock, pages, latency):
    """One server process: aiohttp on uvloop, on the shared socket."""
    import uvloop
    from aiohttp import web

    rng = random.Random(0)
    paragraphs = [" ".join(rng.choices(WORDS, k=520)) for _ in range(24)]

    async def handle(request):
        n = int(request.match_info["n"])
        if not 0 <= n < pages:
            raise web.HTTPNotFound()
        await asyncio.sleep(latency)
        body = gzip.compress(page_html(n, pages, paragraphs), 6)
        return web.Response(body=body, headers={
            "Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"})

    async def main():
        app = web.Application()
        app.router.add_get("/p/{n:[0-9]+}", handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.SockSite(runner, sock, backlog=4096).start()
        await asyncio.Event().wait()

    uvloop.run(main())


def run_server(port, processes, pages, latency):
    """The server's parent process: forks the server processes and waits for SIGTERM."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(4096)
    children = []
    for _ in range(processes):
        if (pid := os.fork()) == 0:
            serve(sock, pages, latency)
            os._exit(0)
        children.append(pid)
    signal.signal(signal.SIGTERM, lambda *_: [os.kill(child, signal.SIGKILL) for child in children])
    for child in children:
        os.waitpid(child, 0)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.05)
    raise SystemExit(f"the server didn't start listening on port {port}")


def cpu_seconds(who):
    usage = resource.getrusage(who)
    return usage.ru_utime + usage.ru_stime


def peak_rss_mib():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 2**20 if sys.platform == "darwin" else peak / 2**10  # bytes on macOS, KiB on Linux


class Limits(logging.Handler):
    """The concurrency law's decisions, from the engine's log: when, which and the new limit."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.decisions = []

    def emit(self, record):
        message = record.getMessage()
        if match := re.search(r"concurrency limit: (.+): \d+ -> (\d+)", message):
            median = re.search(r"median ([0-9.]+) ms", message)
            self.decisions.append((time.perf_counter(), match[1], int(match[2]),
                                   float(median[1]) if median else None))

    def summary(self, started, ended):
        limits = [limit for _, _, limit, _ in self.decisions]
        weighted, at, limit = 0.0, started, 8  # the law starts at 8
        for when, _, new, _ in self.decisions:
            weighted += limit * (when - at)
            at, limit = when, new
        weighted += limit * (ended - at)
        kinds = collections.Counter(kind for _, kind, _, _ in self.decisions)
        medians = [median for *_, median in self.decisions if median is not None]
        return {"lowest": min(limits, default=None), "highest": max(limits, default=None),
                "last": limits[-1] if limits else None,
                "mean_over_time": round(weighted / (ended - started), 1), "decisions": dict(kinds),
                "rounds_median_ttfb_ms": round(statistics.median(medians), 1) if medians else None}


def crawl(args, port):
    import uvloop
    from web_crawler.config import Config
    from web_crawler.engine import crawl
    from web_crawler.output import ThreadedSink

    limits = Limits()
    engine_log = logging.getLogger("web_crawler.engine")
    engine_log.addHandler(limits)
    engine_log.setLevel(logging.INFO)
    workers = None if args.parse_workers == "auto" else int(args.parse_workers)
    cfg = Config(start_url=f"http://127.0.0.1:{port}/p/0", max_concurrency=args.max_concurrency,
                 parse_workers=workers)
    cpu_before = cpu_seconds(resource.RUSAGE_SELF)
    started = time.perf_counter()
    with open(os.devnull, "wb") as devnull:
        report = asyncio.run(crawl(cfg, ThreadedSink(devnull)), loop_factory=uvloop.new_event_loop)
    ended = time.perf_counter()
    elapsed = ended - started
    cpu = cpu_seconds(resource.RUSAGE_SELF) - cpu_before
    stats = report.stats
    return {
        "pages": stats.pages,
        "links_found": stats.links_found,
        "unique_urls": report.unique_urls,
        "seconds": round(elapsed, 2),
        "pages_per_second": round(stats.pages / elapsed, 1),
        "coordinator_cpu_us_per_page": round(cpu / stats.pages * 1e6, 1),
        "coordinator_peak_rss_mib": round(peak_rss_mib(), 1),
        "limit": limits.summary(started, ended),
        "failures": dict(stats.failures),
        "exit_code": int(report.exit_code),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", type=int, default=50_000)
    parser.add_argument("--latency", type=float, default=0.05, help="seconds before each response")
    parser.add_argument("--max-concurrency", type=int, default=64)
    parser.add_argument("--parse-workers", default="auto")
    parser.add_argument("--server-processes", type=int, default=4)
    parser.add_argument("--json", metavar="FILE", help="also write the results here")
    parser.add_argument("--serve", type=int, metavar="PORT", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        run_server(args.serve, args.server_processes, args.pages, args.latency)
        return
    port = free_port()
    server = subprocess.Popen([sys.executable, __file__, "--serve", str(port), "--pages", str(args.pages),
                               "--latency", str(args.latency), "--server-processes", str(args.server_processes)])
    try:
        wait_for(port)
        results = crawl(args, port)
    finally:
        server.terminate()
        server.wait()
    results["setup"] = {
        "pages": args.pages, "latency_s": args.latency, "max_concurrency": args.max_concurrency,
        "parse_workers": args.parse_workers, "server_processes": args.server_processes,
        "machine": platform.machine(), "system": platform.system(), "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }
    text = json.dumps(results, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w") as file:
            file.write(text + "\n")


if __name__ == "__main__":
    main()
