"""Appendix A.12: how widely a real site's response times spread when it is under no load from us.

Usage: python bench/ttfb_spread.py START_URL [N=40]

Fetches START_URL, takes the first N same-host page links on it, and requests them one after another
over one reused connection with curl. Reports time to first byte without connection set-up
(time_starttransfer - time_pretransfer) for the 200 responses, and median / p10: the first draft of
DESIGN.md §5.3 read any ratio above 2 as congestion. Makes N + 1 requests to the site; needs curl.
Give a START_URL that doesn't redirect, since relative links are resolved against it.
"""
import re
import statistics
import subprocess
import sys
from urllib.parse import urljoin, urlsplit

UA = "web-crawler-bench/0.1 (one-off latency sample)"
SKIP = re.compile(r"\.(css|js|png|svg|ico|xml|json|woff2?)(\?|$)")


def main(start, n):
    host = urlsplit(start).netloc
    html = subprocess.run(["curl", "-sSL", "--compressed", "-A", UA, start],
                          capture_output=True, text=True, errors="replace", check=True).stdout
    urls = []
    for href in re.findall(r'href="([^"#]+)', html):
        u = urljoin(start, href.replace("&amp;", "&"))
        if urlsplit(u).netloc == host and u not in urls and not SKIP.search(u):
            urls.append(u)
    cmd = ["curl", "-sS", "--compressed", "-A", UA,
           "-w", "%{http_code} %{time_pretransfer} %{time_starttransfer}\n"]
    for u in urls[:n]:
        cmd += ["-o", "/dev/null", u]
    # check=False: one failed page shouldn't discard the timings of the rest
    lines = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.splitlines()
    ttfb = sorted(float(c) - float(b) for code, b, c in (line.split() for line in lines) if code == "200")
    p10, median, p90 = ttfb[len(ttfb) // 10], statistics.median(ttfb), ttfb[(9 * len(ttfb)) // 10]
    print(f"{host:<22} {len(ttfb):>3} pages  p10 {p10 * 1e3:6.1f} ms  median {median * 1e3:6.1f} ms"
          f"  p90 {p90 * 1e3:6.1f} ms  median/p10 {median / p10:5.2f}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 40)
