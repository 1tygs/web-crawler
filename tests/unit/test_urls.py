import ada_url
import pytest
from hypothesis import given
from hypothesis import strategies as st

from web_crawler.urls import Scope, StartURLError, normalize_start, strip_fragment


@pytest.mark.parametrize(
    ("raw", "url", "implicit"),
    [
        ("example.com", "https://example.com/", True),
        ("localhost:8000", "https://localhost:8000/", True),
        ("127.0.0.1:8000/docs/", "https://127.0.0.1:8000/docs/", True),
        ("[::1]:8000", "https://[::1]:8000/", True),
        ("example.com/a b?q=1 2", "https://example.com/a%20b?q=1%202", True),
        ("http://example.com", "http://example.com/", False),
        ("HTTPS://Example.COM/A#top", "https://example.com/A", False),
        ("https://example.com:443/", "https://example.com/", False),
        ("http://example.com:8080", "http://example.com:8080/", False),
        ("https://bücher.example/straße", "https://xn--bcher-kva.example/stra%C3%9Fe", False),
        # A browser reads these with the scheme as given; prepending https:// would make
        # "https" the host.
        ("https:/example.com", "https://example.com/", False),
        ("http:example.com", "http://example.com/", False),
        # The WHATWG parser trims C0 controls and spaces and drops tabs and newlines.
        ("  https://example.com/\n", "https://example.com/", False),
        ("ht\ttps://exam\nple.com/", "https://example.com/", False),
    ],
)
def test_normalize_start(raw: str, url: str, implicit: bool) -> None:
    assert normalize_start(raw) == (url, implicit)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "isn't a valid URL"),
        ("https://exa mple.com/", "isn't a valid URL"),
        ("http://example.com:99999/", "isn't a valid URL"),
        ("javascript:alert(1)", "isn't a valid URL"),
        ("ftp://example.com/", "isn't an http or https URL"),
        ("file:///etc/passwd", "isn't an http or https URL"),
        ("https://user:secret@example.com/", "contains credentials"),
        ("user@example.com", "contains credentials"),
    ],
)
def test_normalize_start_rejects(raw: str, reason: str) -> None:
    with pytest.raises(StartURLError, match=reason):
        normalize_start(raw)


@pytest.mark.parametrize(
    ("start", "prefixes"),
    [
        # On the default ports both schemes are in scope, whichever one the crawl starts on.
        ("https://example.com/", ("https://example.com/", "http://example.com/")),
        ("http://example.com/a/b?c", ("https://example.com/", "http://example.com/")),
        # Any other port keeps to its own scheme.
        ("http://127.0.0.1:8000/", ("http://127.0.0.1:8000/",)),
        ("https://example.com:8443/", ("https://example.com:8443/",)),
        ("http://[::1]:8000/", ("http://[::1]:8000/",)),
        ("https://[2001:db8::1]/", ("https://[2001:db8::1]/", "http://[2001:db8::1]/")),
    ],
)
def test_scope_prefixes(start: str, prefixes: tuple[str, ...]) -> None:
    scope = Scope.for_url(start)
    assert scope.prefixes == prefixes
    assert scope.origins == tuple(prefix.removesuffix("/") for prefix in prefixes)


@pytest.mark.parametrize(
    ("url", "inside"),
    [
        ("https://example.com/", True),
        ("http://example.com/deep/path?q=1", True),
        ("https://www.example.com/", False),
        ("https://blog.example.com/", False),
        ("https://example.com.evil.net/", False),
        ("https://example.com@evil.net/", False),
        ("https://example.com:8443/", False),
        ("http://example.com:443/", False),
        ("https://example.com./", False),  # a trailing dot is another host to ada-url
        ("https://example.org/", False),
    ],
)
def test_scope_contains(url: str, inside: bool) -> None:
    assert Scope.for_url("https://example.com/").contains(url) is inside


def test_scope_rejects_a_non_http_url() -> None:
    with pytest.raises(ValueError, match="not an http or https URL"):
        Scope.for_url("ftp://example.com/")


HOSTS = ["example.com", "www.example.com", "example.com.evil.net", "127.0.0.1", "[::1]"]
PORTS = ["", ":80", ":443", ":8080"]


@given(
    start=st.tuples(
        st.sampled_from(["http", "https"]), st.sampled_from(HOSTS), st.sampled_from(PORTS)
    ),
    link=st.tuples(
        st.sampled_from(["http", "https"]),
        st.sampled_from(["", "user@", "example.com@", "u:p@"]),
        st.sampled_from(HOSTS),
        st.sampled_from(PORTS),
        st.text(alphabet="/abc%?#:@.", max_size=12),
    ),
)
def test_scope_contains_matches_the_parsed_components(
    start: tuple[str, str, str], link: tuple[str, str, str, str, str]
) -> None:
    # The prefix test must agree with comparing the parsed host, port and scheme.
    start_url = ada_url.URL(f"{start[0]}://{start[1]}{start[2]}/")
    scheme, userinfo, host, port, rest = link
    try:
        url = ada_url.URL(f"{scheme}://{userinfo}{host}{port}/{rest}")
    except ValueError:
        return
    same_service = (
        url.protocol == start_url.protocol and url.port == start_url.port
        if start_url.port
        else url.port == ""
    )
    expected = not url.username and not url.password and url.hostname == start_url.hostname
    expected = expected and same_service
    assert Scope.for_url(start_url.href).contains(strip_fragment(url.href)) is expected


def test_strip_fragment() -> None:
    assert strip_fragment("https://example.com/a?q#f") == "https://example.com/a?q"
    assert strip_fragment("https://example.com/#") == "https://example.com/"
    assert strip_fragment("https://example.com/#a#b") == "https://example.com/"
    url = "https://example.com/plain"
    assert strip_fragment(url) is url
