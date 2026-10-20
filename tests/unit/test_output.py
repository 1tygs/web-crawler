import asyncio
import errno
import io
import os
import threading
from typing import BinaryIO

import pytest

from web_crawler.output import MemorySink, ThreadedSink


class Recorder(io.RawIOBase):
    """A binary stream that records each write() and flush(), and can hold a write back."""

    def __init__(self) -> None:
        self.calls: list[bytes | str] = []
        self.flushed = threading.Event()
        self.writing = threading.Event()  # set while a write is held at the gate
        self.gate = threading.Event()
        self.gate.set()

    def writable(self) -> bool:
        return True

    def write(self, data: object) -> int:
        assert isinstance(data, bytes)
        self.writing.set()
        self.gate.wait()
        self.calls.append(data)
        return len(data)

    def flush(self) -> None:
        self.calls.append("flush")
        self.flushed.set()

    def written(self) -> bytes:
        return b"".join(call for call in self.calls if isinstance(call, bytes))


async def wait_for(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


def backlogged(sink: ThreadedSink) -> bool:
    # A function call, so that mypy doesn't narrow the property between two reads.
    return sink.backlogged


def test_memory_sink_keeps_records() -> None:
    sink = MemorySink()
    sink.start(lambda: None)
    sink.write(b"a\n")
    sink.write(b"b\n")
    assert not sink.backlogged
    sink.close()
    assert sink.records == [b"a\n", b"b\n"]
    assert sink.closed


async def test_writes_every_record_in_order() -> None:
    stream = Recorder()
    sink = ThreadedSink(stream)  # type: ignore[arg-type]
    sink.start(lambda: None)
    records = [f"https://example.com/{n}\n\n".encode() for n in range(1000)]
    for record in records:
        sink.write(record)
    sink.close()
    assert stream.written() == b"".join(records)
    assert stream.calls[-1] == "flush"


async def test_sends_what_queued_up_meanwhile_in_one_write() -> None:
    stream = Recorder()
    stream.gate.clear()
    sink = ThreadedSink(stream)  # type: ignore[arg-type]
    sink.start(lambda: None)
    sink.write(b"first\n")
    await wait_for(stream.writing)  # the writer holds "first" at the gate
    for name in (b"a\n", b"b\n", b"c\n"):
        sink.write(name)
    stream.gate.set()
    sink.close()
    assert [call for call in stream.calls if isinstance(call, bytes)] == [b"first\n", b"a\nb\nc\n"]


async def test_flushes_as_soon_as_the_queue_runs_dry() -> None:
    stream = Recorder()
    sink = ThreadedSink(stream)  # type: ignore[arg-type]
    sink.start(lambda: None)
    sink.write(b"record\n")
    await wait_for(stream.flushed)  # without close(): a reader sees the record now
    assert stream.calls == [b"record\n", "flush"]
    sink.close()


async def test_a_backlog_pauses_until_it_drains() -> None:
    stream = Recorder()
    stream.gate.clear()
    sink = ThreadedSink(stream, high_water=100, low_water=50)  # type: ignore[arg-type]
    loop = asyncio.get_running_loop()
    drained = loop.create_future()
    sink.start(lambda: drained.set_result(threading.current_thread()))
    sink.write(b"x" * 10)
    await wait_for(stream.writing)
    writes = 0
    while not backlogged(sink):
        sink.write(b"y" * 10)
        writes += 1
    assert writes == 10  # 110 bytes queued and not yet written is past the high-water mark
    stream.gate.set()
    assert await asyncio.wait_for(drained, 5) is threading.main_thread()  # on the loop
    assert not backlogged(sink)
    sink.close()
    assert stream.written() == b"x" * 10 + b"y" * 100


def pipe_without_a_reader() -> tuple[BinaryIO, int]:
    read_end, write_end = os.pipe()
    os.close(read_end)
    return open(write_end, "wb"), write_end


async def test_a_closed_pipe_is_quiet_and_discards_the_rest() -> None:
    stream, write_end = pipe_without_a_reader()
    sink = ThreadedSink(stream)
    sink.start(lambda: None)
    for _ in range(100):
        sink.write(b"record\n")
    sink.close()  # no BrokenPipeError
    # The descriptor now points at /dev/null, so the interpreter's own flush at exit works.
    assert os.write(write_end, b"x") == 1
    stream.close()


async def test_a_closed_pipe_without_a_descriptor_is_quiet_too() -> None:
    class Gone(Recorder):
        def write(self, data: object) -> int:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    sink = ThreadedSink(Gone())  # type: ignore[arg-type]
    sink.start(lambda: None)
    sink.write(b"record\n")
    sink.close()


async def test_other_write_errors_surface_on_close() -> None:
    class Full(Recorder):
        def write(self, data: object) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    sink = ThreadedSink(Full())  # type: ignore[arg-type]
    sink.start(lambda: None)
    for _ in range(10):
        sink.write(b"record\n")
    with pytest.raises(OSError, match="No space left on device"):
        sink.close()


def test_close_without_start_does_nothing() -> None:
    ThreadedSink(Recorder()).close()  # type: ignore[arg-type]
