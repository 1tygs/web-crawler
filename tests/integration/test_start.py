"""Start-URL handling and the default ports, end to end (DESIGN.md §2.4, §6.1).

Hosts such as example.test are served locally on their default ports, over http and https,
through tests/support/hosts.py's resolver.
"""

import logging
import ssl
from collections import Counter

import pytest
import trustme
from aiohttp import web

from tests.support.command import CRAWL, run, split_records
from tests.support.hosts import HostedSites
from tests.support.jitter import Lowest
from tests.support.site import Handler, Resource, Site, page, redirect, unused_port
from web_crawler.config import Config
from web_crawler.engine import CrawlReport, crawl
from web_crawler.output import MemorySink
from web_crawler.types import ExitCode

TEXT = (("Content-Type", "text/plain"),)


async def crawl_hosts(
    hosts: HostedSites,
    start: str,
    *,
    parse_workers: int,
    trusted: bool = True,
    strict_host: bool = False,
    insecure: bool = False,
) -> tuple[CrawlReport, list[bytes]]:
    cfg = Config(
        start_url=start, strict_host=strict_host, insecure=insecure, parse_workers=parse_workers
    )
    async with hosts.transport(cfg, trusted=trusted) as transport:
        sink = MemorySink()
        report = await crawl(cfg, sink, transport=transport, rng=Lowest())
    return report, sorted(sink.records)


def record(url: str, *links: str) -> bytes:
    return "".join([url, "\n", *(f"  {link}\n" for link in links), "\n"]).encode()


def https_only(resource: Resource) -> Handler:
    """Serves resource over https, and redirects plain HTTP to it, as most sites do."""

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.scheme != "https":
            location = f"https://{request.host}{request.raw_path}"
            return web.Response(status=301, headers={"Location": location})
        response = web.Response(status=resource.status, body=resource.body)
        response.headers.update(resource.headers)
        return response

    return handle


async def test_http_and_https_on_the_default_ports_are_one_site(parse_workers: int) -> None:
    home = page(
        "/both",
        "http://example.test/plain",  # the other scheme, which serves this page itself
        "https://example.test:443/explicit",  # a default port, which is no port at all
        "http://example.test/both",  # redirects to the https page: crawled once
        "https://blog.example.test/",  # a subdomain: printed, never requested
    )
    resources: dict[str, Resource | Handler] = {
        "/": https_only(home),
        "/both": https_only(page()),
        "/plain": page("/"),  # links to http://example.test/, which redirects home
        "/explicit": https_only(page()),
    }
    async with HostedSites({"example.test": resources, "blog.example.test": {}}) as hosts:
        report, records = await crawl_hosts(
            hosts, "https://example.test/", parse_workers=parse_workers
        )
    assert report.exit_code is ExitCode.OK
    assert records == sorted(
        [
            record(
                "https://example.test/",
                "https://example.test/both",
                "http://example.test/plain",
                "https://example.test/explicit",
                "http://example.test/both",
                "https://blog.example.test/",
            ),
            record("https://example.test/both"),
            record("http://example.test/plain", "http://example.test/"),
            record("https://example.test/explicit"),
        ]
    )
    requested = hosts.requested()
    assert max(Counter(requested).values()) == 1  # no URL requested twice
    assert sorted(requested) == sorted(
        [
            "https://example.test/robots.txt",
            "http://example.test/robots.txt",
            "https://example.test/",
            "https://example.test/both",
            "http://example.test/plain",
            "https://example.test/explicit",
            "http://example.test/both",  # its redirect leads to a claimed page
            "http://example.test/",  # likewise
        ]
    )
    assert report.stats.failures == {"redirect to a URL already claimed": 2}
    assert hosts.sites["blog.example.test"].requests == []


async def test_an_apex_start_url_that_redirects_to_www_crawls_www(
    caplog: pytest.LogCaptureFixture,
    parse_workers: int,
) -> None:
    caplog.set_level(logging.WARNING)
    apex = {"/": redirect("https://www.example.test/")}
    www = {"/": page("/a", "https://example.test/apex"), "/a": page("/")}
    async with HostedSites({"example.test": apex, "www.example.test": www}) as hosts:
        report, records = await crawl_hosts(
            hosts, "https://example.test/", parse_workers=parse_workers
        )
    assert report.exit_code is ExitCode.OK
    assert records == sorted(
        [
            record(
                "https://www.example.test/",
                "https://www.example.test/a",
                "https://example.test/apex",
            ),
            record("https://www.example.test/a", "https://www.example.test/"),
        ]
    )
    assert (
        "the start URL redirects to https://www.example.test/: crawling www.example.test "
        "instead of example.test" in caplog.text
    )
    # www's robots.txt is read before its first page; the apex is out of scope now.
    assert hosts.sites["www.example.test"].urls()[:2] == [
        "https://www.example.test/robots.txt",
        "http://www.example.test/robots.txt",
    ] or hosts.sites["www.example.test"].urls()[:2] == [
        "http://www.example.test/robots.txt",
        "https://www.example.test/robots.txt",
    ]
    assert "https://example.test/apex" not in hosts.requested()


async def test_the_www_hosts_robots_txt_is_obeyed_before_the_hop(
    caplog: pytest.LogCaptureFixture,
    parse_workers: int,
) -> None:
    disallow = Resource(body=b"User-agent: *\nDisallow: /\n", headers=TEXT)
    apex = {"/": redirect("https://www.example.test/")}
    www = {"/robots.txt": disallow, "/": page()}
    async with HostedSites({"example.test": apex, "www.example.test": www}) as hosts:
        report, records = await crawl_hosts(
            hosts, "https://example.test/", parse_workers=parse_workers
        )
    assert report.exit_code is ExitCode.CANNOT_START
    assert records == []
    assert (
        "can't start: https://www.example.test/robots.txt disallows https://www.example.test/ "
        "for web-crawler" in caplog.text
    )
    assert "https://www.example.test/" not in hosts.requested()


async def test_strict_host_refuses_even_the_www_redirect(
    caplog: pytest.LogCaptureFixture, parse_workers: int
) -> None:
    apex = {"/": redirect("https://www.example.test/")}
    async with HostedSites({"example.test": apex, "www.example.test": {"/": page()}}) as hosts:
        report, _ = await crawl_hosts(
            hosts, "https://example.test/", parse_workers=parse_workers, strict_host=True
        )
    assert report.exit_code is ExitCode.CANNOT_START
    assert (
        "can't start: https://example.test/ redirects to https://www.example.test/, and "
        "--strict-host keeps the crawl to example.test; to crawl that host instead, run: "
        "crawl https://www.example.test/" in caplog.text
    )
    assert hosts.sites["www.example.test"].requests == []


async def test_a_redirect_to_another_host_cant_start(
    caplog: pytest.LogCaptureFixture, parse_workers: int
) -> None:
    apex = {"/": redirect("https://blog.example.test/")}
    async with HostedSites({"example.test": apex, "blog.example.test": {"/": page()}}) as hosts:
        report, _ = await crawl_hosts(hosts, "https://example.test/", parse_workers=parse_workers)
    assert report.exit_code is ExitCode.CANNOT_START
    assert (
        "can't start: https://example.test/ redirects to https://blog.example.test/, outside "
        "the crawl's scope; to crawl there instead, run: crawl https://blog.example.test/"
        in caplog.text
    )
    assert hosts.sites["blog.example.test"].requests == []


async def test_a_start_url_without_a_scheme_falls_back_to_http(
    caplog: pytest.LogCaptureFixture,
    parse_workers: int,
) -> None:
    caplog.set_level(logging.WARNING)
    async with HostedSites({"example.test": {"/": page("/a"), "/a": page()}}) as hosts:
        hosts.resolver.ports["example.test", 443] = unused_port()  # nothing listens on https
        report, records = await crawl_hosts(hosts, "example.test", parse_workers=parse_workers)
    assert report.exit_code is ExitCode.OK
    assert records == sorted(
        [
            record("http://example.test/", "http://example.test/a"),
            record("http://example.test/a"),
        ]
    )
    assert (
        "https://example.test/ got no answer (connect); trying http://example.test/, since no "
        "scheme was given" in caplog.text
    )


async def test_only_a_start_url_without_a_scheme_falls_back(
    caplog: pytest.LogCaptureFixture,
    parse_workers: int,
) -> None:
    async with HostedSites({"example.test": {"/": page()}}) as hosts:
        hosts.resolver.ports["example.test", 443] = unused_port()
        report, _ = await crawl_hosts(hosts, "https://example.test/", parse_workers=parse_workers)
    assert report.exit_code is ExitCode.CANNOT_START
    assert "can't start: https://example.test/robots.txt got no answer (connect: " in caplog.text


async def test_tls_certificates_are_verified_unless_insecure(
    caplog: pytest.LogCaptureFixture,
    parse_workers: int,
) -> None:
    async with HostedSites({"example.test": {"/": page()}}) as hosts:
        refused, _ = await crawl_hosts(
            hosts, "https://example.test/", parse_workers=parse_workers, trusted=False
        )
        allowed, records = await crawl_hosts(
            hosts,
            "https://example.test/",
            parse_workers=parse_workers,
            trusted=False,
            insecure=True,
        )
    assert refused.exit_code is ExitCode.CANNOT_START
    assert "can't start: https://example.test/robots.txt got no answer (tls: " in caplog.text
    assert allowed.exit_code is ExitCode.OK
    assert records == [record("https://example.test/")]


async def test_the_crawl_command_falls_back_to_http_for_localhost() -> None:
    # `crawl localhost:PORT` against a plain local server: https fails its TLS handshake.
    async with Site({"/": page("/a"), "/a": page()}) as site:
        code, stdout, stderr = await run(CRAWL, f"localhost:{site.port}")
    assert code == 0, stderr
    origin = f"http://localhost:{site.port}"
    assert split_records(stdout) == sorted(
        [record(f"{origin}/", f"{origin}/a"), record(f"{origin}/a")]
    )
    assert f"https://localhost:{site.port}/ got no answer (tls); trying {origin}/" in stderr


async def test_the_crawl_command_with_insecure_accepts_any_certificate() -> None:
    ca = trustme.CA()  # trusted by nothing
    server_ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1").configure_cert(server_ssl)
    async with Site({"/": page()}, tls=server_ssl) as site:
        code, stdout, _ = await run(CRAWL, "--insecure", f"{site.tls_origin}/")
        refused, _, stderr = await run(CRAWL, f"{site.tls_origin}/")
    assert code == 0
    assert split_records(stdout) == [record(f"{site.tls_origin}/")]
    assert refused == ExitCode.CANNOT_START
    assert "certificate verify failed" in stderr
