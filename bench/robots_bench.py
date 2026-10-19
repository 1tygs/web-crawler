"""Appendix A.6: robots.txt matching cost, Protego vs urllib.robotparser.

Usage: python bench/robots_bench.py FIXTURES_DIR
FIXTURES_DIR holds pairs NAME.html + NAME.robots.txt; HOSTS below maps NAME -> host.
"""
import pathlib
import sys
import time
import urllib.robotparser

import ada_url
from protego import Protego
from selectolax.lexbor import LexborHTMLParser

HOSTS = {"wikipedia": "en.wikipedia.org", "guardian": "www.theguardian.com"}
UA = "web-crawler"
fix = pathlib.Path(sys.argv[1])

for name, host in HOSTS.items():
    rules = (fix / f"{name}.robots.txt").read_text("utf-8", "replace")
    urls = []
    for n in LexborHTMLParser((fix / f"{name}.html").read_bytes()).css("a[href]"):
        try:
            urls.append(ada_url.join_url(f"https://{host}/x/y", n.attributes.get("href")))
        except ValueError:
            pass
    urls = [u for u in urls if host in u] * 20

    protego = Protego.parse(rules)
    t0 = time.perf_counter()
    allowed = sum(protego.can_fetch(u, UA) for u in urls)
    t_protego = (time.perf_counter() - t0) / len(urls)

    std = urllib.robotparser.RobotFileParser()
    std.parse(rules.splitlines())
    t0 = time.perf_counter()
    allowed_std = sum(std.can_fetch(UA, u) for u in urls)
    t_std = (time.perf_counter() - t0) / len(urls)

    print(f"{name:<10} {len(rules):>6} B  protego {t_protego * 1e6:5.1f} µs/url (allowed {allowed}/{len(urls)})"
          f"  urllib.robotparser {t_std * 1e6:5.1f} µs/url (allowed {allowed_std})")
