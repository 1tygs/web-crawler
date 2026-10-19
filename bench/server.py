"""Appendix A.4 test server: pre-fork aiohttp + uvloop, serves one pre-gzipped page on any path.

Usage: python bench/server.py PAGE.html [WORKERS=6]     (listens on 127.0.0.1:8899)
"""
import gzip, os, socket, sys, signal
from aiohttp import web
import uvloop

body = gzip.compress(open(sys.argv[1], "rb").read(), 6)
workers = int(sys.argv[2]) if len(sys.argv) > 2 else 6
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 8899)); sock.listen(4096); sock.set_inheritable(True)

async def handler(request):
    return web.Response(body=body, headers={"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"})

def serve():
    app = web.Application(); app.router.add_route("GET", "/{tail:.*}", handler)
    uvloop.run(_main(app))

async def _main(app):
    runner = web.AppRunner(app, access_log=None); await runner.setup()
    site = web.SockSite(runner, sock); await site.start()
    import asyncio; await asyncio.Event().wait()

pids = []
for _ in range(workers):
    pid = os.fork()
    if pid == 0:
        serve(); os._exit(0)
    pids.append(pid)
print(f"serving {len(body)} gzip bytes on :8899 with {workers} workers", flush=True)
signal.signal(signal.SIGTERM, lambda *a: [os.kill(p, signal.SIGTERM) for p in pids] or sys.exit(0))
for p in pids: os.waitpid(p, 0)
