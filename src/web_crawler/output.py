"""Where records go: the Sink protocol and the writer thread behind stdout (DESIGN.md §5.8).

Records arrive whole and already rendered, so a sink only writes them. ThreadedSink makes
its blocking writes on a thread of its own, so a slow reader never blocks the event loop
(DESIGN.md D17).
"""

import asyncio
import contextlib
import io
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import BinaryIO, Protocol

MIB = 1024 * 1024


class Sink(Protocol):
    """Where the crawl's records go. Every method is called from the event loop's thread."""

    def start(self, on_drained: Callable[[], object]) -> None:
        """Begin accepting records. on_drained runs on the event loop when a backlog clears."""
        ...

    def write(self, record: bytes) -> None: ...

    @property
    def backlogged(self) -> bool:
        """True while the sink is too far behind to take more records."""
        ...

    def close(self) -> None:
        """Write everything accepted so far, then stop."""
        ...


class ThreadedSink:
    """Writes records to a binary stream from a dedicated thread.

    Each batch of queued records goes out in one write(). The stream is flushed whenever the
    queue runs dry, and at least every 100 ms while it doesn't, so `| head` and `tail -f`
    see records promptly. Past 64 MiB of backlog, backlogged turns on until the backlog
    falls below 32 MiB, when on_drained runs.

    The backlog is the difference of two counters, bytes queued and bytes written, each
    written by one thread only, so no lock is needed.
    """

    FLUSH_INTERVAL = 0.1

    def __init__(
        self, stream: BinaryIO, *, high_water: int = 64 * MIB, low_water: int = 32 * MIB
    ) -> None:
        self._stream = stream
        self._high_water = high_water
        self._low_water = low_water
        self._queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._queued = 0  # bytes; written only by the event loop's thread
        self._written = 0  # bytes; written only by the writer thread
        self._waiting = False  # the event loop wants on_drained once the backlog clears
        self._discarding = False  # the reader is gone, or a write failed
        self._error: OSError | None = None
        self._thread: threading.Thread | None = None

    def start(self, on_drained: Callable[[], object]) -> None:
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._run, args=(loop, on_drained), name="crawl-output", daemon=True
        )
        self._thread.start()

    def write(self, record: bytes) -> None:
        self._queued += len(record)
        self._queue.put(record)

    @property
    def backlogged(self) -> bool:
        if self._queued - self._written <= self._high_water:
            return False
        self._waiting = True
        # The writer checks _waiting after each batch. It may have emptied the queue since
        # the first check, before seeing the flag, so check again or the wake-up is lost.
        return self._queued - self._written >= self._low_water

    def close(self) -> None:
        """Write and flush everything queued, then stop the thread. A write error other
        than a closed pipe is raised here."""
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join()
        if self._error is not None:
            raise self._error

    def _run(self, loop: asyncio.AbstractEventLoop, on_drained: Callable[[], object]) -> None:
        last_flush = time.monotonic()
        while True:
            batch = [self._queue.get()]
            with contextlib.suppress(queue.Empty):
                while True:
                    batch.append(self._queue.get_nowait())
            done = batch[-1] is None  # close() queues None last
            records = [record for record in batch if record is not None]
            data = b"".join(records)
            self._send(data)
            self._written += len(data)
            now = time.monotonic()
            if done or self._queue.empty() or now - last_flush >= self.FLUSH_INTERVAL:
                self._send(None)
                last_flush = now
            if self._waiting and self._queued - self._written < self._low_water:
                self._waiting = False
                loop.call_soon_threadsafe(on_drained)
            if done:
                return

    def _send(self, data: bytes | None) -> None:
        """Write data, or flush when it's None. After a failure, discard everything."""
        if self._discarding:
            return
        try:
            if data is None:
                self._stream.flush()
            elif data:
                self._stream.write(data)
        except BrokenPipeError:
            # The reader went away, e.g. `| head`: discard the rest quietly. Pointing the
            # descriptor at /dev/null keeps the interpreter's own flush at exit from failing.
            self._discarding = True
            self._stream_to_devnull()
        except OSError as error:
            self._discarding = True
            self._error = error

    def _stream_to_devnull(self) -> None:
        try:
            fd = self._stream.fileno()
        except OSError, io.UnsupportedOperation:
            return
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            os.close(devnull)


class MemorySink:
    """Keeps records in a list, for tests and for callers that want them in memory."""

    def __init__(self) -> None:
        self.records: list[bytes] = []
        self.closed = False

    def start(self, on_drained: Callable[[], object]) -> None:
        pass

    def write(self, record: bytes) -> None:
        self.records.append(record)

    @property
    def backlogged(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True
