"""Appendix A.16: what admission costs per URL it hasn't seen, with its rules.

Admission runs its rules once per new URL: the trap rules, the extension filter and robots.txt
(DESIGN.md §5.1). This times Admission.admit on generated URLs, as a crawl's pages hand them
over, in batches of 120, with no robots.txt and with a short one. Run it on two checkouts to
compare them.

Usage: uv run python bench/admission_bench.py [--urls N] [--repeat R]
"""

import argparse
import random
import statistics
import time

from web_crawler.frontier import Admission, Frontier
from web_crawler.robots import RobotsPolicy, RobotsTxt
from web_crawler.urls import Scope, dedupe_key

WORDS = ("docs library asyncio blog 2026 10 news about guide api reference tutorial page "
         "index.html item user search").split()
ROBOTS = b"User-agent: *\nDisallow: /search\nDisallow: /api/private\nAllow: /api\n"
ORIGIN = "https://www.example.com"


def make_links(count: int) -> list[tuple[str, str]]:
    """count distinct URLs of 1-5 path segments: 15% with a query, and 5% of the rest with
    an extension, a few of them for files that are almost never HTML."""
    rng = random.Random(1)
    urls: set[str] = set()
    while len(urls) < count:
        segments = (rng.choice(WORDS) + str(rng.randrange(50)) for _ in range(rng.randrange(1, 6)))
        path = "/".join(segments)
        if rng.random() < 0.15:
            path += f"?page={rng.randrange(2000)}"
        elif rng.random() < 0.05:
            path += rng.choice([".pdf", ".png", ".css", ".js", ".html"])
        urls.add(f"{ORIGIN}/{path}")
    return [(dedupe_key(url), url) for url in sorted(urls)]


def time_admission(links: list[tuple[str, str]], policy: RobotsPolicy | None, repeat: int) -> list[float]:
    """Microseconds per URL for each of `repeat` runs, each admitting every link once."""
    scope = Scope.for_url(f"{ORIGIN}/")
    times = []
    for _ in range(repeat):
        admission = Admission(Frontier(), scope, policy)
        started = time.perf_counter()
        for i in range(0, len(links), 120):
            admission.admit(links[i : i + 120], depth=1)
        times.append((time.perf_counter() - started) / len(links) * 1e6)
    return times


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--urls", type=int, default=200_000)
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    links = make_links(args.urls)
    file = RobotsTxt.found(f"{ORIGIN}/robots.txt", ROBOTS, truncated=False)
    policies = {"no robots.txt": None, "a robots.txt": RobotsPolicy("web-crawler", {ORIGIN: file})}
    for label, policy in policies.items():
        times = time_admission(links, policy, args.repeat)
        print(f"{label:>14}: {statistics.median(times):.2f} µs per new URL "
              f"(min {min(times):.2f}, median of {args.repeat})")


if __name__ == "__main__":
    main()
