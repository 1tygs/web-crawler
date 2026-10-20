"""Waiting, in tests, for what another process does."""

import asyncio
from collections.abc import Callable

PATIENCE = 10.0  # seconds before giving up


async def until(condition: Callable[[], bool]) -> None:
    """Return once condition() is true, checking every 10 ms; fail after PATIENCE."""
    async with asyncio.timeout(PATIENCE):
        while not condition():  # noqa: ASYNC110  (another process sets it: no Event to wait on)
            await asyncio.sleep(0.01)
