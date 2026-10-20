"""One HTTP exchange at a time, headers before body (DESIGN.md §5.2, §6.2).

The fetcher works against the Transport protocol, and AiohttpTransport implements it.
Nothing above this module imports aiohttp: its exceptions arrive as TransportError.
"""

import contextvars
import logging
import ssl
import time
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

import aiohttp
import yarl
from aiohttp.client_reqrep import ClientRequest
from aiohttp.connector import Connection
from aiohttp.tracing import Trace
from multidict import CIMultiDictProxy

from web_crawler.config import Config
from web_crawler.types import ErrorKind

log = logging.getLogger(__name__)

KIB = 1024

# The types the crawler parses, and the content codings content.decode_body can undo
# (DESIGN.md §5.2).
ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
ACCEPT_ENCODING = "gzip, deflate, br, zstd"


class TransportError(Exception):
    """A request that failed without an HTTP response, or whose body couldn't be read."""

    def __init__(self, kind: ErrorKind, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind


class Response(Protocol):
    """One HTTP response. The status and headers are there before any of the body is read."""

    @property
    def url(self) -> str: ...
    @property
    def status(self) -> int: ...
    @property
    def headers(self) -> Mapping[str, str]:
        """Case-insensitive. A field sent more than once has its values joined with ", ", as
        HTTP defines (RFC 9110 §5.3)."""
        ...

    @property
    def ttfb(self) -> float:
        """Time to first byte: seconds from when the request had a connection, pooled or new,
        until its headers arrived. Connection set-up, DNS and TLS included, isn't counted."""
        ...

    async def read(self, limit: int) -> tuple[bytes, bool]:
        """Up to limit more bytes of the body, as sent: still compressed, as the
        Content-Encoding header says. Also whether any are left after them. Repeated calls
        continue where the last one stopped."""
        ...


class Transport(Protocol):
    def open(self, url: str) -> AbstractAsyncContextManager[Response]:
        """GET url, as given, without following redirects. Leaving the context reuses the
        connection if the body was read to the end, and closes it otherwise."""
        ...

    async def close(self) -> None: ...


class AiohttpTransport:
    """Transport over an aiohttp keep-alive pool. Create it inside the event loop.

    resolver and ssl_context are for tests: a resolver that sends test hosts' default ports
    to local servers, and a context that trusts their certificates. Otherwise TLS
    certificates are verified, unless cfg.insecure.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        resolver: aiohttp.abc.AbstractResolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        # Four connections beyond the concurrency limit, for requests the dispatcher doesn't
        # count, such as robots.txt refetches and health probes.
        connections = cfg.max_concurrency + 4
        self._session = aiohttp.ClientSession(
            connector=_TimedConnector(
                limit=connections,
                limit_per_host=connections,
                ttl_dns_cache=300,
                keepalive_timeout=30,
                resolver=resolver,
                ssl=ssl_context if ssl_context is not None else not cfg.insecure,
            ),
            headers={
                "User-Agent": cfg.user_agent,
                "Accept": ACCEPT,
                "Accept-Encoding": ACCEPT_ENCODING,
            },
            # The parser workers decompress bodies, off the event loop, which halves its CPU
            # per request (DESIGN.md D8).
            auto_decompress=False,
            # Browsers accept cookies from IP-address hosts too; aiohttp needs unsafe=True.
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=cfg.timeout, sock_connect=10, sock_read=20),
            # The defaults, 8,190-byte lines and fields and 128 headers, fail pages that send
            # a large Set-Cookie or Content-Security-Policy header.
            max_line_size=64 * KIB,
            max_field_size=64 * KIB,
            max_headers=256,
            # Proxies come from the environment through _EnvProxies. trust_env=True would
            # re-read them, and ~/.netrc, on a thread for every request: 162.5 µs of CPU per
            # request rather than 26.9 (DESIGN.md A.4). It would also send ~/.netrc
            # credentials to the crawled host.
            trust_env=False,
        )
        self._proxies = _EnvProxies()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    def open(self, url: str) -> AbstractAsyncContextManager[Response]:
        return _Exchange(self._session, self._proxies, url)

    async def close(self) -> None:
        await self._session.close()


class _Exchange:
    """The context manager AiohttpTransport.open returns."""

    __slots__ = ("_proxies", "_response", "_session", "_url")
    _response: aiohttp.ClientResponse  # set by __aenter__; __aexit__ runs only if that succeeded

    def __init__(self, session: aiohttp.ClientSession, proxies: _EnvProxies, url: str) -> None:
        self._session = session
        self._proxies = proxies
        self._url = url

    async def __aenter__(self) -> _AiohttpResponse:
        try:
            # encoded=True sends the URL exactly as given. Otherwise yarl rewrites it:
            # ?b=%2f becomes ?b=/, %7E becomes ~ and %zz becomes %25zz (plan §2).
            target = yarl.URL(self._url, encoded=True)
            route = self._proxies.route(target)
            started = time.perf_counter()
            self._response = await self._session.get(
                target,
                allow_redirects=False,
                proxy=route.proxy,
                headers=route.headers,
                proxy_headers=route.proxy_headers,
            )
            ttfb = time.perf_counter() - max(started, _connected.get(started))
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise transport_error(error) from error
        return _AiohttpResponse(self._url, self._response, ttfb)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # aiohttp closes the connection instead of pooling it unless the body was read to
        # the end.
        self._response.release()
        await self._response.wait_for_close()


# When the connector last handed a connection to a request of this task. The connector runs
# in the task that makes the request, so it's that request's.
_connected: contextvars.ContextVar[float] = contextvars.ContextVar("connected")


class _TimedConnector(aiohttp.TCPConnector):
    """Notes when it hands a request its connection: at once from the pool, or once a new
    one is up, through DNS, TCP, TLS and any proxy. A response's time to first byte counts
    from then (DESIGN.md §5.2), as aiohttp's TraceConfig could also report, but at 1.3 µs of
    CPU per request rather than 8.3 (DESIGN.md A.4)."""

    async def connect(  # aiohttp's own signature, timeout and all
        self,
        req: ClientRequest,
        traces: list[Trace],
        timeout: aiohttp.ClientTimeout,  # noqa: ASYNC109
    ) -> Connection:
        connection = await super().connect(req, traces, timeout)
        _connected.set(time.perf_counter())
        return connection


class _AiohttpResponse:
    __slots__ = ("_content", "_pending", "headers", "status", "ttfb", "url")

    def __init__(self, url: str, response: aiohttp.ClientResponse, ttfb: float) -> None:
        self.url = url
        self.status = response.status
        self.headers: Mapping[str, str] = _Headers(response.headers)
        self.ttfb = ttfb
        self._content = response.content
        self._pending = b""  # read past the last limit, to know whether more was left

    async def read(self, limit: int) -> tuple[bytes, bool]:
        chunks = [self._pending]
        size = len(self._pending)
        try:
            while size <= limit:
                chunk = await self._content.read(limit + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise transport_error(error) from error
        body = b"".join(chunks)
        if size > limit:
            self._pending = body[limit:]
            return body[:limit], True
        self._pending = b""
        return body, False


class _Headers(Mapping[str, str]):
    """aiohttp's headers, with a field sent more than once read as one value: its values
    joined with ", ", in order (RFC 9110 §5.3). aiohttp's own lookup gives the first."""

    __slots__ = ("_fields",)

    def __init__(self, fields: CIMultiDictProxy[str]) -> None:
        self._fields = fields

    def __getitem__(self, name: str) -> str:
        values = self._fields.getall(name)
        return values[0] if len(values) == 1 else ", ".join(values)

    def __iter__(self) -> Iterator[str]:
        """Each field's name once, as first sent."""
        names: dict[str, str] = {}
        for name in self._fields:
            names.setdefault(name.lower(), name)
        return iter(names.values())

    def __len__(self) -> int:
        return len({name.lower() for name in self._fields})


@dataclass(frozen=True, slots=True)
class _Route:
    """How a request reaches its host: directly, or through a proxy with its credentials."""

    proxy: yarl.URL | None = None
    headers: dict[str, str] | None = None  # added to the request itself
    proxy_headers: dict[str, str] | None = None  # sent to the proxy when tunnelling


_DIRECT = _Route()


class _EnvProxies:
    """HTTP proxies from the environment (HTTP_PROXY, HTTPS_PROXY, NO_PROXY), read once.

    It follows aiohttp's trust_env rules, except that it doesn't read ~/.netrc or the
    operating system's proxy settings: a proxy URL's userinfo is its credentials, and only
    http:// proxies are used.
    """

    def __init__(self) -> None:
        self._env = urllib.request.getproxies_environment()
        self._routes: dict[str, _Route] = {}
        for scheme in ("http", "https"):
            if not (value := self._env.get(scheme)):
                continue
            proxy = yarl.URL(value)
            if proxy.scheme != "http":
                log.warning("ignoring %s_proxy=%s: only http:// proxies work", scheme, value)
                continue
            if proxy.user is None:
                self._routes[scheme] = _Route(proxy)
                continue
            credentials = {
                "Proxy-Authorization": aiohttp.encode_basic_auth(proxy.user, proxy.password or "")
            }
            # aiohttp 3.14 deprecates proxy_auth, but the proxy_headers it recommends instead
            # only reach the proxy on the CONNECT that tunnels https://. An http:// request
            # goes to the proxy whole, so it carries the credentials itself.
            if scheme == "http":
                self._routes[scheme] = _Route(proxy.with_user(None), headers=credentials)
            else:
                self._routes[scheme] = _Route(proxy.with_user(None), proxy_headers=credentials)
        self._by_host: dict[tuple[str, str | None], _Route] = {}

    def route(self, url: yarl.URL) -> _Route:
        if not self._routes:
            return _DIRECT
        key = (url.scheme, url.raw_host)
        try:
            return self._by_host[key]
        except KeyError:
            pass
        route = self._routes.get(url.scheme, _DIRECT)
        if url.raw_host is not None and _bypasses(url.raw_host, self._env.get("no", "")):
            route = _DIRECT
        self._by_host[key] = route
        return route


def _bypasses(host: str, no_proxy: str) -> bool:
    """Whether NO_PROXY exempts host, as urllib reads it: "*" exempts every host, and each
    comma-separated name exempts itself and its subdomains."""
    if no_proxy == "*":
        return True
    host = host.lower()
    for entry in no_proxy.split(","):
        name = entry.strip().lstrip(".").lower()
        if name and (host == name or host.endswith(f".{name}")):
            return True
    return False


def transport_error(error: BaseException) -> TransportError:
    """The TransportError for an exception aiohttp or yarl raised. Order matters: the
    aiohttp exception classes overlap."""
    match error:
        case aiohttp.ClientConnectorDNSError():
            kind = ErrorKind.DNS
        case aiohttp.ClientSSLError():
            kind = ErrorKind.TLS
        case aiohttp.ConnectionTimeoutError():
            kind = ErrorKind.CONNECT_TIMEOUT
        case aiohttp.ClientConnectorError():
            kind = ErrorKind.CONNECT
        case TimeoutError():  # the total timeout, and aiohttp's socket read timeout
            kind = ErrorKind.TIMEOUT
        case aiohttp.ClientPayloadError():
            kind = ErrorKind.PAYLOAD
        case aiohttp.ClientConnectionError() | ConnectionError():
            kind = ErrorKind.RESET
        case aiohttp.InvalidURL() | aiohttp.NonHttpUrlClientError() | ValueError():
            kind = ErrorKind.INVALID_URL
        case _:
            kind = ErrorKind.PROTOCOL
    return TransportError(kind, str(error) or type(error).__name__)
