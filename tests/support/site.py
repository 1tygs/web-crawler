"""An in-process HTTP site for tests, with a log of every request it served.

Resources are keyed by the raw request target, exactly as it arrives in the request line, so
a test can tell /a?b=%2f from /a?b=/. A site given a TLS context serves the same resources
over https too, on a port of its own.
"""

import asyncio
import html
import socket
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from aiohttp import web

HTML = "text/html; charset=utf-8"


@dataclass(frozen=True, slots=True)
class Resource:
    status: int = 200
    body: bytes = b""
    # Sent in order, and only these: a name given twice is sent as two fields, and with no
    # Content-Type there's none, as aiohttp would otherwise add.
    headers: tuple[tuple[str, str], ...] = (("Content-Type", HTML),)


type Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def page(*hrefs: str) -> Resource:
    """An HTML page with one <a> per href, in order. hrefs are escaped as attribute values."""
    links = "".join(f'<li><a href="{html.escape(href)}">link</a></li>\n' for href in hrefs)
    return Resource(body=f"<!doctype html>\n<title>t</title>\n<ul>\n{links}</ul>\n".encode())


def redirect(location: str, status: int = 301) -> Resource:
    return Resource(status=status, headers=(("Location", location),))


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    target: str  # the raw request target, as sent
    headers: Mapping[str, str]
    peer: tuple[str, int]  # the client's address: the same port means a reused connection
    scheme: str = "http"

    @property
    def url(self) -> str:
        """The URL requested, from the scheme, the Host header and the target."""
        return f"{self.scheme}://{self.headers['Host']}{self.target}"


@dataclass(slots=True)
class Site:
    """Serves resources on http://127.0.0.1:PORT, and 404 for any other target; with tls,
    on https://127.0.0.1:TLS_PORT as well."""

    resources: Mapping[str, Resource | Handler]
    tls: ssl.SSLContext | None = None
    requests: list[Request] = field(default_factory=list)
    port: int = 0
    tls_port: int = 0
    _runner: web.AppRunner | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def tls_origin(self) -> str:
        return f"https://127.0.0.1:{self.tls_port}"

    def url(self, target: str = "/") -> str:
        return self.origin + target

    def targets(self) -> list[str]:
        return [request.target for request in self.requests]

    def urls(self) -> list[str]:
        return [request.url for request in self.requests]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        self.requests.append(
            Request(
                request.method,
                request.raw_path,
                dict(request.headers),
                tuple(peer or ("", 0)),
                request.scheme,
            )
        )
        resource = self.resources.get(request.raw_path)
        if resource is None:
            return web.Response(
                status=404, body=b"<!doctype html>not found", content_type="text/html"
            )
        if not isinstance(resource, Resource):
            return await resource(request)
        response = _Response(status=resource.status, body=resource.body)
        sent: set[str] = set()
        for name, value in resource.headers:
            if name.lower() in sent:  # a field sent more than once
                response.headers.add(name, value)
            else:
                response.headers[name] = value
                sent.add(name.lower())
        response.typed = "content-type" in sent
        return response

    async def __aenter__(self) -> Self:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        # Handlers still running when the test ends are cancelled after shutdown_timeout,
        # rather than awaited, so a deliberately slow handler doesn't slow the suite.
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)
        await self._runner.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        await web.SockSite(self._runner, sock).start()
        if self.tls is not None:
            tls_sock = socket.socket()
            tls_sock.bind(("127.0.0.1", 0))
            self.tls_port = tls_sock.getsockname()[1]
            await web.SockSite(self._runner, tls_sock, ssl_context=self.tls).start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


class _Response(web.Response):
    """Sends a Resource's fields as given. aiohttp adds a Content-Type to any body that has
    none, which a page without a type, to be sniffed, mustn't get."""

    typed = True

    async def _prepare_headers(self) -> None:
        await super()._prepare_headers()
        if not self.typed:
            self.headers.popall("Content-Type", None)


def unused_port() -> int:
    """A port nothing listens on, at least until something else binds it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(slots=True)
class RawServer:
    """Answers each connection with fixed bytes, for responses no HTTP server would send."""

    reply: bytes
    close: bool = True  # close the connection after replying, rather than wait for the client
    port: int = 0
    received: list[bytes] = field(default_factory=list)  # each request's head, as sent
    _server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            self.received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(self.reply)
            await writer.drain()
            if not self.close:
                await reader.read()  # until the client hangs up
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()
