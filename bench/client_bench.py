"""Appendix A.4: HTTP client requests/s and client CPU per request at fixed concurrency.

Usage: python bench/client_bench.py CLIENT CONCURRENCY TOTAL   (with bench/server.py running)
CLIENT: aiohttp | aiohttp+uvloop | aiohttp+uvloop+raw | httpx | curl_cffi | niquests
"""
import asyncio, sys, time, resource

NAME, CONC, TOTAL = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
URL = "http://127.0.0.1:8899/p/"

def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF); return r.ru_utime + r.ru_stime

async def drive(fetch):
    counter = iter(range(TOTAL)); nbytes = 0
    async def worker():
        nonlocal nbytes
        for i in counter:
            n = await fetch(URL + str(i)); nbytes += n
    await asyncio.gather(*(worker() for _ in range(CONC)))
    return nbytes

async def main():
    if NAME.startswith("aiohttp"):
        import aiohttp
        raw = "raw" in NAME
        conn = aiohttp.TCPConnector(limit=CONC, limit_per_host=CONC, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=conn, auto_decompress=not raw) as s:
            async def fetch(u):
                async with s.get(u) as r: return len(await r.read())
            await fetch(URL + "warm")
            return await timed(fetch)
    if NAME == "httpx":
        import httpx
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=CONC, max_keepalive_connections=CONC)) as c:
            async def fetch(u): return len((await c.get(u)).content)
            await fetch(URL + "warm")
            return await timed(fetch)
    if NAME == "curl_cffi":
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(max_clients=CONC) as s:
            async def fetch(u): return len((await s.get(u)).content)
            await fetch(URL + "warm")
            return await timed(fetch)
    if NAME == "niquests":
        import niquests
        async with niquests.AsyncSession(pool_connections=1, pool_maxsize=CONC) as s:
            async def fetch(u): return len((await s.get(u)).content)
            await fetch(URL + "warm")
            return await timed(fetch)
    raise SystemExit(f"unknown client {NAME}")

async def timed(fetch):
    c0, t0 = cpu(), time.perf_counter()
    nbytes = await drive(fetch)
    dt, dc = time.perf_counter() - t0, cpu() - c0
    return dt, dc, nbytes

if __name__ == "__main__":
    loop_kind = "uvloop" if "uvloop" in NAME else "asyncio"
    if loop_kind == "uvloop":
        import uvloop; dt, dc, nb = uvloop.run(main())
    else:
        dt, dc, nb = asyncio.run(main())
    print(f"{NAME:<22} conc={CONC:<4} {TOTAL/dt:9.0f} req/s   {dc/TOTAL*1e6:7.1f} µs CPU/req   body={nb//TOTAL} B", flush=True)
