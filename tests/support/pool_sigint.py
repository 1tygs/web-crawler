"""Ctrl-C while the parser pool's workers start, for tests/integration/test_pool.py.

Usage: python -m tests.support.pool_sigint DELAY

Submits eight parses to a pool of eight, so that each starts a worker, then sends SIGINT to
the process group DELAY seconds later, as a terminal's Ctrl-C does. Like a crawl's
coordinator, it handles the signal itself and carries on: it prints, as JSON, how many
parses came back and whether the signal arrived.
"""

import asyncio
import json
import logging
import os
import signal
import sys

from web_crawler.pool import ProcessParser
from web_crawler.types import ParseJob, WorkerContext


async def main(delay: float) -> None:
    interrupted = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, interrupted.set)
    parser = ProcessParser(WorkerContext(("https://example.com/",), 1 << 20), 8)
    jobs = [ParseJob(f"https://example.com/{n}", b"<a href=/x>x</a>") for n in range(8)]
    parses = [asyncio.create_task(parser.parse(job)) for job in jobs]
    await asyncio.sleep(0)  # each parse is submitted, and its worker starting
    await asyncio.sleep(delay)
    os.killpg(os.getpgrp(), signal.SIGINT)
    results = await asyncio.gather(*parses, return_exceptions=True)
    await parser.close()
    parsed = sum(not isinstance(result, BaseException) for result in results)
    print(json.dumps({"parsed": parsed, "interrupted": interrupted.is_set()}))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)  # the pool's warnings, to stderr
    asyncio.run(main(float(sys.argv[1])))
