"""Appendix A.2/A.3: full parse-pipeline cost per page, and coordinator cost with/without a process pool.

Usage: python bench/pipeline_bench.py FIXTURES_DIR

parse_page() here is a measurement prototype of the worker contract in DESIGN.md §5.5:
compressed body in -> (rendered output bytes, in-scope dedupe keys) out. It omits charset
sniffing (UTF-8 fast path only) and returns keys rather than (key, url) pairs.
"""
import asyncio, gzip, os, re, sys, time, resource, pathlib, zlib, statistics
from concurrent.futures import ProcessPoolExecutor
import brotli, ada_url, orjson
from compression import zstd
from selectolax.lexbor import LexborHTMLParser

MAX_OUT = 32 * 1024 * 1024
NAV_SEL = "a[href], area[href], iframe[src], frame[src], link[href][rel], meta[http-equiv]"
LINK_RELS = {"canonical", "alternate", "next", "prev", "previous"}
_PCT = re.compile(r"%[0-9A-Fa-f]{2}")
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_REFRESH = re.compile(r"^\s*\d*(?:\.\d*)?\s*[;,]?\s*(?:url\s*=\s*)?(['\"]?)(.+?)\1\s*$", re.I)

def _pct(m):
    v = int(m.group(0)[1:], 16)
    return chr(v) if v in _UNRESERVED else m.group(0).upper()

def dedupe_key(url):  # RFC 3986 6.2.2.2 on top of WHATWG serialization
    return _PCT.sub(_pct, url) if "%" in url else url

def decompress(body, enc):
    if not enc or enc == "identity": return body[:MAX_OUT]
    if enc in ("gzip", "x-gzip", "deflate"):
        wbits = 47 if enc != "deflate" else 15  # 47 = auto gzip/zlib header
        d = zlib.decompressobj(wbits); out = d.decompress(body, MAX_OUT); return out
    if enc == "br": return brotli.Decompressor().process(body, output_buffer_limit=MAX_OUT)
    if enc == "zstd": return zstd.ZstdDecompressor().decompress(body, max_length=MAX_OUT)
    raise ValueError(enc)

def _attrs_links(root, out):
    for n in root.css(NAV_SEL):
        tag = n.tag; a = n.attributes
        if tag in ("a", "area"): v = a.get("href")
        elif tag in ("iframe", "frame"): v = a.get("src")
        elif tag == "link":
            rel = (a.get("rel") or "").lower().split()
            v = a.get("href") if LINK_RELS.intersection(rel) else None
        else:  # meta refresh
            if (a.get("http-equiv") or "").lower() != "refresh": continue
            m = _REFRESH.match(a.get("content") or ""); v = m.group(2) if m else None
        if v is not None: out.append(v)
    for frag in root.template_fragments():  # declarative shadow DOM is rendered; plain <template> is inert
        p = frag.parent
        if p is not None and "shadowrootmode" in p.attributes: _attrs_links(frag, out)
    return out

def parse_page(page_url, content_encoding, body, scope_prefixes, fmt="text", depth=0):
    html = decompress(body, content_encoding)
    tree = LexborHTMLParser(html)  # UTF-8 fast path (charset sniffing omitted in proto)
    base = page_url
    b = tree.css_first("base[href]")
    if b is not None:
        try: base = ada_url.join_url(page_url, b.attributes.get("href") or "")
        except ValueError: pass
    seen, links, keys = set(), [], []
    for raw in _attrs_links(tree, []):
        try: u = ada_url.join_url(base, raw)
        except ValueError: continue
        if not u.startswith(("https:", "http:")): continue
        h = u.find("#")
        if h != -1: u = u[:h]
        if u in seen: continue
        seen.add(u); links.append(u)
        if u.startswith(scope_prefixes): keys.append(dedupe_key(u))
    if fmt == "jsonl":
        out = orjson.dumps({"url": page_url, "depth": depth, "links": links}) + b"\n"
    else:
        out = (page_url + "\n" + "".join("  " + l + "\n" for l in links) + "\n").encode()
    return out, keys

# ---------------------------------------------------------------- benchmarks
def load_pages(fix):
    pages = []
    for p in sorted(pathlib.Path(fix).glob("*.html")):
        raw = p.read_bytes()
        host = f"www.{p.stem}.org"
        pages.append((f"https://{host}/some/dir/page", raw, (f"https://{host}/", f"http://{host}/")))
    return pages

def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF); return r.ru_utime + r.ru_stime

def bench_inline(pages):
    print("per-page worker CPU (inline, single core):")
    for url, raw, scope in pages:
        variants = {"gzip": gzip.compress(raw, 6), "br": brotli.compress(raw, quality=5), "zstd": zstd.compress(raw, 3), None: raw}
        row = []
        for enc, body in variants.items():
            parse_page(url, enc, body, scope)
            n = 200; t0 = time.perf_counter()
            for _ in range(n): out, keys = parse_page(url, enc, body, scope)
            row.append(f"{str(enc):>5}={((time.perf_counter()-t0)/n)*1e3:6.2f}ms")
        print(f"  {url.split('.')[1]:<10} {len(raw)//1024:>5} KB  links={len(out.decode().splitlines())-2:<4} in-scope={len(keys):<4} " + "  ".join(row))

async def coordinator(pages, workers, total):
    """Simulates the coordinator: submit compressed bodies, receive results, dedupe, write."""
    loop = asyncio.get_running_loop()
    bodies = [(u, "gzip", gzip.compress(r, 6), s) for u, r, s in pages]
    pool = ProcessPoolExecutor(max_workers=workers) if workers else None
    if pool:  # warm workers
        await asyncio.gather(*(loop.run_in_executor(pool, parse_page, *bodies[0]) for _ in range(workers * 2)))
    seen = set(); sink = open(os.devnull, "wb", buffering=1 << 20)
    sem = asyncio.Semaphore(max(4, workers * 4))
    async def one(i):
        u, e, b, s = bodies[i % len(bodies)]
        async with sem:
            if pool: out, keys = await loop.run_in_executor(pool, parse_page, u, e, b, s)
            else: out, keys = parse_page(u, e, b, s)
        for k in keys:
            if k not in seen: seen.add(k)
        sink.write(out)
    c0, t0 = cpu(), time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(total)))
    dt, dc = time.perf_counter() - t0, cpu() - c0
    if pool: pool.shutdown()
    return total / dt, dc / total * 1e6

if __name__ == "__main__":
    import uvloop
    pages = load_pages(sys.argv[1])
    bench_inline(pages)
    print("\ncoordinator: pages/s and coordinator-process CPU per page (gzip bodies, mixed pages):")
    for w in (0, 1, 2, 4, 8):
        rate, us = uvloop.run(coordinator(pages, w, 4000 if w else 2000))
        print(f"  workers={w}  {rate:8.0f} pages/s   coordinator CPU {us:7.1f} µs/page")
