"""A scripted Transport for the fetcher's and the engine's tests: no sockets, exact control."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from web_crawler.transport import ErrorKind, TransportError


@dataclass(slots=True)
class Reply:
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=lambda: {"Content-Type": "text/html"})
    body: bytes = b""
    delay: float = 0.0  # seconds before the headers arrive
    read_error: ErrorKind | None = None  # raised by read(), as when a body is cut off


@dataclass(slots=True)
class FakeResponse:
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    read_error: ErrorKind | None
    position: int = 0  # how far the body has been read

    async def read(self, limit: int) -> tuple[bytes, bool]:
        if self.read_error is not None:
            raise TransportError(self.read_error, "scripted")
        chunk = self.body[self.position : self.position + limit]
        self.position += len(chunk)
        return chunk, self.position < len(self.body)


type Answer = Reply | ErrorKind


@dataclass(slots=True)
class FakeTransport:
    """Answers each URL from replies: a Reply, or an ErrorKind to fail with, or a list of
    them for successive requests, the last repeating. Any other URL gets a 404. It records
    every response it gave, and the most requests ever in flight."""

    replies: Mapping[str, Answer | list[Answer]]
    responses: list[FakeResponse] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    closed: bool = False
    _asked: Counter[str] = field(default_factory=Counter)

    @property
    def requested(self) -> list[str]:
        return [response.url for response in self.responses]

    @asynccontextmanager
    async def open(self, url: str) -> AsyncIterator[FakeResponse]:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            reply = self.replies.get(url, Reply(status=404))
            if isinstance(reply, list):
                reply = reply[min(self._asked[url], len(reply) - 1)]
            self._asked[url] += 1
            if isinstance(reply, ErrorKind):
                await asyncio.sleep(0)
                raise TransportError(reply, "scripted")
            await asyncio.sleep(reply.delay)
            response = FakeResponse(url, reply.status, reply.headers, reply.body, reply.read_error)
            self.responses.append(response)
            yield response
        finally:
            self.in_flight -= 1

    async def close(self) -> None:
        self.closed = True
