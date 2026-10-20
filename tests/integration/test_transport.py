import asyncio
import gzip
import zlib
from compression import zstd

import aiohttp
import brotli
import pytest
import yarl
from aiohttp import web

from tests.support.site import RawServer, Resource, Site, page, redirect, unused_port
from web_crawler.config import USER_AGENT, Config
from web_crawler.transport import (
    ACCEPT,
    ACCEPT_ENCODING,
    AiohttpTransport,
    ErrorKind,
    TransportError,
    transport_error,
)


def make_transport(timeout: float = 60.0) -> AiohttpTransport:
    return AiohttpTransport(Config(start_url="http://127.0.0.1/", timeout=timeout))


async def fetch(transport: AiohttpTransport, url: str) -> tuple[int, bytes]:
    async with transport.open(url) as response:
        body, more = await response.read(1 << 20)
        assert not more
        return response.status, body


# yarl rewrites each of these unless it's told the URL is already encoded (plan §2).
REWRITTEN_BY_YARL = ["/x?b=%2f", "/%7Euser/", "/a/%2E%2E/b", "/bad%zz", "/q?x=%7e&y=%41"]


async def test_requests_the_url_byte_for_byte() -> None:
    async with Site({target: page() for target in REWRITTEN_BY_YARL}) as site:
        for target in REWRITTEN_BY_YARL:
            assert str(yarl.URL(site.url(target))) != site.url(target)
        async with make_transport() as transport:
            for target in REWRITTEN_BY_YARL:
                assert (await fetch(transport, site.url(target)))[0] == 200
    assert site.targets() == REWRITTEN_BY_YARL


async def test_sends_the_crawlers_headers() -> None:
    async with Site({"/": page()}) as site, make_transport() as transport:
        await fetch(transport, site.url())
    headers = site.requests[0].headers
    assert headers["User-Agent"] == USER_AGENT
    assert headers["Accept"] == ACCEPT
    assert headers["Accept-Encoding"] == ACCEPT_ENCODING


async def test_returns_redirects_without_following_them() -> None:
    async with (
        Site({"/old": redirect("/new"), "/new": page()}) as site,
        make_transport() as transport,
        transport.open(site.url("/old")) as response,
    ):
        assert response.status == 301
        assert response.headers["location"] == "/new"
        assert response.url == site.url("/old")
    assert site.targets() == ["/old"]


async def test_read_continues_where_it_stopped() -> None:
    async with (
        Site({"/": Resource(body=b"0123456789")}) as site,
        make_transport() as transport,
        transport.open(site.url()) as response,
    ):
        assert await response.read(4) == (b"0123", True)
        assert await response.read(4) == (b"4567", True)
        assert await response.read(4) == (b"89", False)
        assert await response.read(4) == (b"", False)


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(9, (b"012345678", True)), (10, (b"0123456789", False)), (11, (b"0123456789", False))],
)
async def test_read_says_exactly_whether_more_is_left(
    limit: int, expected: tuple[bytes, bool]
) -> None:
    async with (
        Site({"/": Resource(body=b"0123456789")}) as site,
        make_transport() as transport,
        transport.open(site.url()) as response,
    ):
        assert await response.read(limit) == expected


async def test_reuses_a_connection_only_after_reading_to_the_end() -> None:
    big = Resource(body=b"x" * 1_000_000)
    async with Site({"/a": big, "/b": big, "/c": big}) as site, make_transport() as transport:
        await fetch(transport, site.url("/a"))
        async with transport.open(site.url("/b")) as response:
            await response.read(10)  # leaves almost all of the body unread
        await fetch(transport, site.url("/c"))
    first, second, third = (request.peer for request in site.requests)
    assert second == first  # /a was read to the end, so its connection carried /b
    assert third != second  # /b wasn't, so its connection was closed


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("br", brotli.compress),
        ("zstd", zstd.compress),
    ],
)
async def test_decodes_compressed_bodies(encoding: str, compress: object) -> None:
    body = b"<!doctype html>" + b"<p>compressible</p>" * 1000
    assert callable(compress)
    resource = Resource(
        body=compress(body), headers=(("Content-Type", "text/html"), ("Content-Encoding", encoding))
    )
    async with Site({"/": resource}) as site, make_transport() as transport:
        assert await fetch(transport, site.url()) == (200, body)


async def test_accepts_headers_beyond_aiohttps_default_limits() -> None:
    # A 20 KB Content-Security-Policy and 200 headers fail aiohttp's defaults.
    headers = b"".join(b"X-%d: v\r\n" % i for i in range(200))
    reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nCSP: " + b"a" * 20_000 + b"\r\n" + headers
    async with RawServer(reply + b"\r\nok") as server, make_transport() as transport:
        assert await fetch(transport, f"http://127.0.0.1:{server.port}/") == (200, b"ok")


async def slow(request: web.Request) -> web.StreamResponse:
    await asyncio.sleep(5)
    return web.Response()


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (b"", ErrorKind.RESET),  # closed before any response
        (b"garbage\r\n\r\n", ErrorKind.PROTOCOL),
        (b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70_000 + b"\r\n\r\n", ErrorKind.PROTOCOL),
    ],
)
async def test_maps_a_bad_response(reply: bytes, kind: ErrorKind) -> None:
    async with RawServer(reply) as server, make_transport() as transport:
        with pytest.raises(TransportError) as error:
            await fetch(transport, f"http://127.0.0.1:{server.port}/")
    assert error.value.kind is kind


async def test_maps_a_body_cut_off() -> None:
    reply = b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nonly this"
    async with (
        RawServer(reply) as server,
        make_transport() as transport,
        transport.open(f"http://127.0.0.1:{server.port}/") as response,
    ):
        with pytest.raises(TransportError) as error:
            await response.read(1 << 20)
    assert error.value.kind is ErrorKind.PAYLOAD


async def test_maps_a_timeout_waiting_for_headers() -> None:
    async with Site({"/": slow}) as site, make_transport(timeout=0.2) as transport:
        with pytest.raises(TransportError) as error:
            await fetch(transport, site.url())
    assert error.value.kind is ErrorKind.TIMEOUT


async def test_maps_a_timeout_reading_the_body() -> None:
    reply = b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\npartial"
    async with (
        RawServer(reply, close=False) as server,
        make_transport(timeout=0.2) as transport,
        transport.open(f"http://127.0.0.1:{server.port}/") as response,
    ):
        with pytest.raises(TransportError) as error:
            await response.read(1 << 20)
    assert error.value.kind is ErrorKind.TIMEOUT


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        (f"http://127.0.0.1:{unused_port()}/", ErrorKind.CONNECT),
        # .invalid never resolves (RFC 2606), offline or not.
        ("http://crawler-test.invalid/", ErrorKind.DNS),
        ("http://[::1/", ErrorKind.INVALID_URL),
        ("ftp://example.com/", ErrorKind.INVALID_URL),
    ],
)
async def test_maps_a_failure_to_connect(url: str, kind: ErrorKind) -> None:
    async with make_transport() as transport:
        with pytest.raises(TransportError) as error:
            await fetch(transport, url)
    assert error.value.kind is kind


async def test_maps_a_failed_tls_handshake() -> None:
    async with Site({"/": page()}) as site, make_transport() as transport:
        with pytest.raises(TransportError) as error:
            await fetch(transport, f"https://127.0.0.1:{site.port}/")  # it speaks plain HTTP
    assert error.value.kind is ErrorKind.TLS


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (aiohttp.ConnectionTimeoutError("connect"), ErrorKind.CONNECT_TIMEOUT),
        (aiohttp.SocketTimeoutError("read"), ErrorKind.TIMEOUT),
        (TimeoutError(), ErrorKind.TIMEOUT),
        (aiohttp.ServerDisconnectedError(), ErrorKind.RESET),
        (aiohttp.ClientOSError(104, "Connection reset by peer"), ErrorKind.RESET),
        (ConnectionResetError(), ErrorKind.RESET),
        (aiohttp.ClientPayloadError("cut off"), ErrorKind.PAYLOAD),
        (aiohttp.InvalidUrlClientError("http:///x"), ErrorKind.INVALID_URL),
        (aiohttp.NonHttpUrlClientError("ftp://x/"), ErrorKind.INVALID_URL),
        (ValueError("Port out of range"), ErrorKind.INVALID_URL),
        (aiohttp.ClientError("other"), ErrorKind.PROTOCOL),
    ],
)
def test_transport_error_kinds(error: BaseException, kind: ErrorKind) -> None:
    mapped = transport_error(error)
    assert mapped.kind is kind
    assert str(mapped).startswith(f"{kind}: ")


async def test_uses_an_http_proxy_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    async with Site({"http://example.test/page?q=%2f": page()}) as proxy:
        monkeypatch.setenv("HTTP_PROXY", f"http://user:secret@127.0.0.1:{proxy.port}")
        async with make_transport() as transport:
            assert (await fetch(transport, "http://example.test/page?q=%2f"))[0] == 200
    [request] = proxy.requests
    assert request.target == "http://example.test/page?q=%2f"  # absolute form, as given
    assert request.headers["Proxy-Authorization"] == aiohttp.encode_basic_auth("user", "secret")


async def test_authenticates_to_the_proxy_that_tunnels_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with RawServer(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n") as proxy:
        monkeypatch.setenv("HTTPS_PROXY", f"http://user:secret@127.0.0.1:{proxy.port}")
        async with make_transport() as transport:
            with pytest.raises(TransportError):
                await fetch(transport, "https://example.test/")
    [head] = proxy.received
    assert head.startswith(b"CONNECT example.test:443 HTTP/1.1\r\n")
    credentials = aiohttp.encode_basic_auth("user", "secret").encode()
    assert b"\r\nProxy-Authorization: " + credentials + b"\r\n" in head


@pytest.mark.parametrize(
    ("no_proxy", "bypassed"),
    [
        ("*", True),
        ("example.test", True),
        (".example.test", True),
        ("other.test, EXAMPLE.TEST ", True),
        ("test", True),  # a parent domain
        ("ample.test", False),  # a suffix that isn't a whole label
        ("www.example.test", False),
        ("", False),
    ],
)
async def test_no_proxy_exempts_hosts(
    no_proxy: str, bypassed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Site({"http://example.test/": page()}) as proxy:
        monkeypatch.setenv("HTTP_PROXY", proxy.origin)
        monkeypatch.setenv("NO_PROXY", no_proxy)
        async with make_transport() as transport:
            if bypassed:
                with pytest.raises(TransportError) as error:  # tries example.test directly
                    await fetch(transport, "http://example.test/")
                assert error.value.kind is ErrorKind.DNS
            else:
                await fetch(transport, "http://example.test/")
    assert len(proxy.requests) == (0 if bypassed else 1)


async def test_ignores_an_https_proxy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with Site({"/": page()}) as site:
        monkeypatch.setenv("HTTP_PROXY", "https://proxy.invalid:3128")
        async with make_transport() as transport:
            assert (await fetch(transport, site.url()))[0] == 200
    assert "only http:// proxies work" in caplog.text
