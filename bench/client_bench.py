"""Appendix A.4: HTTP client requests/s and client CPU per request at fixed concurrency.

Usage: python bench/client_bench.py CLIENT CONCURRENCY TOTAL   (with bench/server.py running)
CLIENT: aiohttp | aiohttp+uvloop | aiohttp+uvloop+raw | httpx | curl_cffi | niquests
        add +env to an aiohttp client for trust_env=True (proxies and ~/.netrc from the environment)
        add +trace to an aiohttp client to time each response from when its connection is ready,
        as aiohttp's TraceConfig reports it; add +ready to time it from when the connector hands
        the connection over, as web_crawler.transport does
"""
import asyncio, contextvars, sys, time, resource, types

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
        raw = "raw" in NAME; env = "env" in NAME; trace = "trace" in NAME; ready = "ready" in NAME
        connector = ready_connector(aiohttp) if ready else aiohttp.TCPConnector
        conn = connector(limit=CONC, limit_per_host=CONC, ttl_dns_cache=300)
        traces = [connection_ready_trace(aiohttp)] if trace else []
        async with aiohttp.ClientSession(connector=conn, auto_decompress=not raw, trust_env=env,
                                         trace_configs=traces) as s:
            if trace:
                async def fetch(u):
                    timing = types.SimpleNamespace(ready=0.0)
                    started = time.perf_counter()
                    async with s.get(u, trace_request_ctx=timing) as r:
                        r.ttfb = time.perf_counter() - max(started, timing.ready)
                        return len(await r.read())
            elif ready:
                async def fetch(u):
                    timing = types.SimpleNamespace(ready=0.0)
                    READY.set(timing)
                    async with s.get(u) as r:
                        r.ttfb = time.perf_counter() - timing.ready
                        return len(await r.read())
            else:
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

READY = contextvars.ContextVar("READY")

def ready_connector(aiohttp):
    """A connector that notes when it hands a request its connection: at once from the pool, or
    once a new one is up."""
    class ReadyConnector(aiohttp.TCPConnector):
        async def connect(self, *args, **kwargs):
            connection = await super().connect(*args, **kwargs)
            READY.get().ready = time.perf_counter()
            return connection
    return ReadyConnector

def connection_ready_trace(aiohttp):
    """The two hooks web_crawler.transport sets: when a connection is ready, new or freed."""
    async def ready(session, ctx, params):
        ctx.trace_request_ctx.ready = time.perf_counter()
    config = aiohttp.TraceConfig()
    config.on_connection_create_end.append(ready)
    config.on_connection_queued_end.append(ready)
    return config

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
