"""Appendix A.7: memory per URL for the seen set and a frontier entry.

Usage: python bench/memory_bench.py
"""
import collections
import random
import string
import tracemalloc

random.seed(1)
N = 1_000_000


def url(i):
    seg = lambda: "".join(random.choices(string.ascii_lowercase + "-", k=random.randint(4, 14)))  # noqa: E731
    return f"https://www.example.com/{seg()}/{seg()}/{seg()}-{i}" + (f"?page={i % 50}" if i % 4 == 0 else "")


tracemalloc.start()
seen = {url(i) for i in range(N)}
after_set = tracemalloc.get_traced_memory()[0]
frontier = collections.deque((u, 3) for u in seen)  # (url, depth); url string shared with the set
after_frontier = tracemalloc.get_traced_memory()[0]

print(f"average URL length: {sum(map(len, seen)) / N:.0f} chars")
print(f"seen set:       {after_set / N:.0f} B/URL  ({after_set / 2**20:.0f} MiB per {N:,} URLs)")
print(f"frontier entry: {(after_frontier - after_set) / N:.0f} B/URL")
