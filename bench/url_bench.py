"""Appendix A.5: URL resolution correctness on tricky hrefs and per-link cost on real pages.

Usage: python bench/url_bench.py FIXTURES_DIR
"""
import sys, time, pathlib, statistics
from urllib.parse import urljoin, urlsplit
import yarl, ada_url
from selectolax.lexbor import LexborHTMLParser

BASE = "https://example.com/dir/page.html"
TRICKY = [
    "  /path with space  ", "\\about", "/a/./b/../c", "//EXAMPLE.com:443/x",
    "https://bücher.example/straße", "?q=a b", "/café", "java\nscript:alert(1)",
    "HTTP://Example.COM/A", "/%7euser", "../../../../x", "/a\tb", "",
    "http://example.com:80/", "https://example.com\\evil.com/", "/a?b=%2f&c=%2F",
]
def ada_join(b, h):
    try: return ada_url.join_url(b, h)
    except ValueError: return "<invalid>"
def yarl_join(b, h):
    try: return str(yarl.URL(b).join(yarl.URL(h)))
    except Exception as e: return f"<{type(e).__name__}>"
print(f"{'href':<34}{'urllib.parse.urljoin':<44}{'yarl':<44}{'ada-url (WHATWG)'}")
for h in TRICKY:
    print(f"{h!r:<34}{urljoin(BASE, h)!r:<44}{yarl_join(BASE, h)!r:<44}{ada_join(BASE, h)!r}")

# per-link cost on real hrefs
hrefs = []
for p in sorted(pathlib.Path(sys.argv[1]).glob("*.html")):
    hrefs += [(f"https://www.{p.stem}.org/some/dir/page", n.attributes.get("href")) for n in LexborHTMLParser(p.read_bytes()).css("a[href]")]
print(f"\n{len(hrefs)} real hrefs")

def t_urljoin():
    for b, h in hrefs: urljoin(b, h)
def t_urljoin_split():
    for b, h in hrefs: urlsplit(urljoin(b, h)).hostname
def t_yarl():
    cache = {}
    for b, h in hrefs:
        bu = cache.get(b) or cache.setdefault(b, yarl.URL(b))
        try: bu.join(yarl.URL(h)).host
        except Exception: pass
def t_ada_join():
    for b, h in hrefs:
        try: ada_url.join_url(b, h)
        except ValueError: pass
def t_ada_join_parse():
    for b, h in hrefs:
        try:
            u = ada_url.join_url(b, h)
            ada_url.parse_url(u, attributes=("hostname", "protocol"))
        except ValueError: pass
def t_ada_URL_obj():
    for b, h in hrefs:
        try:
            u = ada_url.URL(h, base=b); u.href; u.hostname
        except ValueError: pass
for fn in (t_urljoin, t_urljoin_split, t_yarl, t_ada_join, t_ada_join_parse, t_ada_URL_obj):
    try:
        fn(); ts=[]
        for _ in range(7):
            t0=time.perf_counter(); fn(); ts.append(time.perf_counter()-t0)
        print(f"{fn.__name__:<20}{statistics.median(ts)/len(hrefs)*1e6:7.2f} µs/link")
    except Exception as e:
        print(f"{fn.__name__:<20} failed: {type(e).__name__}: {e}")
