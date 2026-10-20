"""A coordinator killed with its parser pool running, for tests/integration/test_pool.py.

Usage: python -m tests.support.pool_orphans

Starts two workers, prints their process IDs as JSON, and waits to be killed.
"""

import asyncio
import json

from tests.support.workers import worker_pid
from web_crawler.pool import ProcessParser
from web_crawler.types import ParseJob, WorkerContext


async def main() -> None:
    parser = ProcessParser(WorkerContext(("https://example.com/",), 1 << 20), 2, target=worker_pid)
    jobs = [ParseJob(f"https://example.com/{n}?sleep=0.2", b"") for n in range(2)]
    results = await asyncio.gather(*map(parser.parse, jobs))
    print(json.dumps([int(result.record) for result in results]), flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
