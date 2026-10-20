"""Test hosts on their default ports: example.test and its neighbours, served locally.

A crawl of https://example.test/ is in scope on both default ports, may redirect to
www.example.test, and links to other hosts. HostedSites serves each host from a Site over
http and https, with a certificate from a throwaway CA, and its resolver sends each host's
ports 80 and 443 to them: aiohttp connects to the port the resolver gives (plan §2). .test
can't be a real domain (RFC 6761), so nothing leaves the machine.
"""

import socket
import ssl
from collections.abc import Mapping
from types import TracebackType
from typing import Self

import trustme
from aiohttp.abc import AbstractResolver, ResolveResult

from tests.support.site import Handler, Resource, Site
from web_crawler.config import Config
from web_crawler.transport import AiohttpTransport


class LocalResolver(AbstractResolver):
    """Resolves (host, port) pairs to local ports; any other host doesn't resolve."""

    def __init__(self) -> None:
        self.ports: dict[tuple[str, int], int] = {}

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        try:
            local = self.ports[host, port]
        except KeyError:
            raise OSError(f"{host}:{port} isn't a test host") from None
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": local,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        pass


class HostedSites:
    """A Site for each host, on its default ports through resolver, over http and https.

    client_ssl trusts the sites' certificates; a transport without it, as a real crawl
    would have, doesn't."""

    def __init__(self, resources: Mapping[str, Mapping[str, Resource | Handler]]) -> None:
        self.ca = trustme.CA()
        server_ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.ca.issue_cert(*resources).configure_cert(server_ssl)
        self.client_ssl = ssl.create_default_context()
        self.ca.configure_trust(self.client_ssl)
        self.sites = {
            host: Site(host_resources, server_ssl) for host, host_resources in resources.items()
        }
        self.resolver = LocalResolver()

    def transport(self, cfg: Config, *, trusted: bool = True) -> AiohttpTransport:
        return AiohttpTransport(
            cfg, resolver=self.resolver, ssl_context=self.client_ssl if trusted else None
        )

    def requested(self) -> list[str]:
        """Every URL requested, from every host, in the order each host saw them."""
        return [url for site in self.sites.values() for url in site.urls()]

    async def __aenter__(self) -> Self:
        for host, site in self.sites.items():
            await site.__aenter__()
            self.resolver.ports[host, 80] = site.port
            self.resolver.ports[host, 443] = site.tls_port
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for site in self.sites.values():
            await site.__aexit__(exc_type, exc, tb)
