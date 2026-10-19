"""Appendix A.1: time to extract every <a href> from real pages, per parser.

Usage: python bench/parse_bench.py FIXTURES_DIR   (a folder of saved *.html pages)
"""
import pathlib
import statistics
import sys
import time
from html.parser import HTMLParser as StdHTMLParser

import lxml.html
from bs4 import BeautifulSoup
from selectolax.lexbor import LexborHTMLParser

FIX = pathlib.Path(sys.argv[1])


def lexbor(b):
    return [n.attributes.get("href") for n in LexborHTMLParser(b).css("a[href]")]


def lxml_xpath(b):
    return lxml.html.fromstring(b).xpath("//a/@href")


def bs4_lxml(s):
    return [a["href"] for a in BeautifulSoup(s, "lxml").find_all("a", href=True)]


def bs4_stdlib(s):
    return [a["href"] for a in BeautifulSoup(s, "html.parser").find_all("a", href=True)]


class _Std(StdHTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.out += [v for k, v in attrs if k == "href" and v is not None]


def stdlib(s):
    p = _Std()
    p.feed(s)
    p.close()
    return p.out


CANDIDATES = [  # (name, function, takes bytes?)
    ("lexbor", lexbor, True),
    ("lxml", lxml_xpath, True),
    ("html.parser", stdlib, False),
    ("bs4+lxml", bs4_lxml, False),
    ("bs4+html.parser", bs4_stdlib, False),
]


def median_time(fn, arg, budget=1.5, min_runs=5):
    fn(arg)
    times, deadline = [], time.perf_counter() + budget
    while len(times) < min_runs or (time.perf_counter() < deadline and len(times) < 2000):
        t0 = time.perf_counter()
        fn(arg)
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


print(f"{'page':<11}{'KB':>6}  " + "".join(f"{name:>22}" for name, _, _ in CANDIDATES))
for page in sorted(FIX.glob("*.html")):
    b = page.read_bytes()
    s = b.decode("utf-8", "replace")
    cells = []
    for _, fn, takes_bytes in CANDIDATES:
        arg = b if takes_bytes else s
        cells.append(f"{median_time(fn, arg) * 1e3:8.2f} ms ({len(fn(arg)):>4})")
    print(f"{page.stem:<11}{len(b) // 1024:>6}  " + "".join(f"{c:>22}" for c in cells))
