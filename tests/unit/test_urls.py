import re

import ada_url
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from web_crawler.urls import (
    Scope,
    StartURLError,
    dedupe_key,
    is_www_variant,
    normalize_start,
    strip_fragment,
)


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


@pytest.mark.parametrize(
    ("start", "href", "inside"),
    [
        # www. and other subdomains are other hosts (DESIGN.md §2.4), and so is a parent.
        ("https://example.com/", "https://www.example.com/", False),
        ("https://www.example.com/", "https://example.com/", False),
        ("https://example.com/", "https://blog.example.com/", False),
        ("https://example.com/", "//a.b.example.com/x", False),
        ("https://blog.example.com/", "https://example.com/", False),
        # Hosts compare after IDNA, whichever form either side is written in.
        ("https://bücher.example/", "https://xn--bcher-kva.example/x", True),
        ("https://xn--bcher-kva.example/", "https://BÜCHER.example/x", True),
        ("https://XN--BCHER-KVA.example/", "/x", True),
        ("https://bücher.example/", "https://bucher.example/", False),
        # Fullwidth letters, which IDNA maps to ASCII.
        ("https://example.com/", "https://\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45.com/", True),
        ("https://example.com/", "https://ex%61mple.com/", True),  # an escaped letter
        # Scheme and host are case-insensitive; the path isn't, but every path is in scope.
        ("https://Example.COM/", "HTTPS://EXAMPLE.com/Path", True),
        # A trailing dot makes another host (pinned, plan §2).
        ("https://example.com/", "https://example.com./", False),
        ("https://example.com./", "https://example.com/", False),
        ("https://example.com./", "/x", True),
        # On the default ports, both schemes; an explicit default port is no port at all.
        ("https://example.com/", "https://example.com:443/", True),
        ("https://example.com/", "http://example.com:80/x", True),
        ("https://example.com/", "https://example.com:8443/", False),
        ("https://example.com/", "http://example.com:443/", False),
        ("https://example.com/", "//example.com:80/x", False),  # port 80, over https
        # Any other port: that port and that scheme only.
        ("http://example.com:8080/", "//example.com:8080/x", True),
        ("http://example.com:8080/", "https://example.com:8080/", False),
        ("http://example.com:8080/", "http://example.com/", False),
        ("http://example.com:8080/", "http://example.com:80/", False),
        # IP addresses compare in their canonical form.
        ("http://127.0.0.1:8000/", "http://2130706433:8000/x", True),
        ("http://127.0.0.1:8000/", "http://0x7f.1:8000/x", True),
        ("http://[::1]:8000/", "http://[0:0::1]:8000/x", True),
        ("http://127.0.0.1:8000/", "http://localhost:8000/", False),
        # Credentials are out of scope, though WHATWG drops an empty userinfo.
        ("https://example.com/", "https://user@example.com/", False),
        ("https://example.com/", "https://user:secret@example.com/", False),
        ("https://example.com/", "https://:@example.com/x", True),
        ("https://example.com/", "https://example.com@evil.example/", False),
        # Either scheme on the default ports, wherever the crawl starts.
        ("https://example.com/", "http://example.com/x", True),
        ("http://example.com/", "https://example.com/x", True),
        # Look-alikes, and URLs that aren't http(s).
        ("https://example.com/", "https://example.com.evil.example/", False),
        ("https://example.com/", "https://evil.example/https://example.com/", False),
        ("https://example.com/", "https://evil.example/?https://example.com/", False),
        ("https://example.com/", "ftp://example.com/", False),
        ("https://example.com/", "javascript:location='https://example.com/'", False),
    ],
)
def test_scope_edge_cases(start: str, href: str, inside: bool) -> None:
    # As parse_page sees them: the start URL normalized, the href resolved against it.
    start_url, _ = normalize_start(start)
    url = strip_fragment(ada_url.join_url(start_url, href))
    assert Scope.for_url(start_url).contains(url) is inside


BOTH = ("https://www.example.com", "http://www.example.com")


@pytest.mark.parametrize(
    ("start", "target", "origins"),
    [
        # Adding or removing "www." moves the crawl, on the default ports with both schemes.
        ("https://example.com/", "https://www.example.com/x", BOTH),
        ("https://example.com/", "http://www.example.com/", BOTH),
        (
            "https://www.example.com/",
            "https://example.com/",
            ("https://example.com", "http://example.com"),
        ),
        (
            "http://example.com:8080/",
            "http://www.example.com:8080/",
            ("http://www.example.com:8080",),
        ),
        # Changing the port, or the scheme of an explicit port, is another service.
        ("http://example.com:8080/", "https://www.example.com:8080/", None),
        ("https://example.com/", "https://www.example.com:8443/", None),
        ("http://example.com:8080/", "http://www.example.com/", None),
        # Any other host, or the same one, isn't a switch.
        ("https://example.com/", "https://blog.example.com/", None),
        ("https://example.com/", "https://www2.example.com/", None),
        ("https://example.com/", "https://www.www.example.com/", None),
        ("https://example.com/", "https://example.com/other", None),
        ("https://example.com/", "https://wwwexample.com/", None),
        # Nor is a URL that isn't http(s), or doesn't parse.
        ("https://example.com/", "ftp://www.example.com/", None),
        ("https://example.com/", "https://www.exa mple.com/", None),
        ("https://127.0.0.1/", "https://www.127.0.0.1/", None),
    ],
)
def test_www_switch(start: str, target: str, origins: tuple[str, ...] | None) -> None:
    switched = Scope.for_url(start).www_switch(target)
    assert (switched and switched.origins) == origins


def test_is_www_variant() -> None:
    assert is_www_variant("example.com", "www.example.com")
    assert is_www_variant("www.example.com", "example.com")
    assert not is_www_variant("example.com", "example.com")
    assert not is_www_variant("example.com", "www2.example.com")
    assert not is_www_variant("example.com", "www.example.org")


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


@pytest.mark.parametrize(
    ("url", "key"),
    [
        # Escapes of unreserved characters are those characters (RFC 3986 §6.2.2.2).
        ("https://example.com/%7Euser/", "https://example.com/~user/"),
        ("https://example.com/%7euser/", "https://example.com/~user/"),
        ("https://example.com/%2D%2E%5F%7E", "https://example.com/-._~"),
        ("https://example.com/%41%7a%30", "https://example.com/Az0"),
        ("https://example.com/?q=%41%62", "https://example.com/?q=Ab"),
        # Other escapes are case-insensitive (§6.2.2.1), but stay escapes: %2F isn't /.
        ("https://example.com/a%2fb?c=%2f", "https://example.com/a%2Fb?c=%2F"),
        ("https://example.com/caf%c3%a9", "https://example.com/caf%C3%A9"),
        ("https://example.com/?q=a%2bb%20c", "https://example.com/?q=a%2Bb%20c"),
        ("https://example.com/%25", "https://example.com/%25"),
        # A "%" that starts no escape stays as it is, and stays distinct from %25.
        ("https://example.com/100%", "https://example.com/100%"),
        ("https://example.com/%zz%7e", "https://example.com/%zz~"),
        ("https://example.com/%%7e", "https://example.com/%~"),
        # Decoding the digits after one would make an escape that isn't in the URL: they
        # stay as escapes, so /%%34%31 (a "%", "4" and "1") never merges with /%41 (an A).
        ("https://example.com/%%34%31", "https://example.com/%%341"),
        ("https://example.com/%4%31", "https://example.com/%%341"),
        ("https://example.com/%%34g", "https://example.com/%4g"),
    ],
)
def test_dedupe_key(url: str, key: str) -> None:
    assert dedupe_key(url) == key


def test_dedupe_key_of_a_url_without_escapes_is_the_url_itself() -> None:
    url = "https://example.com/a/b?c=d"
    assert dedupe_key(url) is url


_PIECE = re.compile(r"%([0-9A-Fa-f]{2})|(.)", re.DOTALL)
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_BARE_PERCENT = ("%",)  # a "%" that starts no escape: neither %25 nor any character


def meaning(url: str) -> list[str | tuple[str]]:
    """What url says under RFC 3986's equivalences: its characters, each escape of an
    unreserved character read as that character, and every other escape in uppercase."""
    pieces: list[str | tuple[str]] = []
    for match in _PIECE.finditer(url):
        if (hex_digits := match[1]) is not None:
            char = chr(int(hex_digits, 16))
            pieces.append(char if char in _UNRESERVED else f"%{hex_digits.upper()}")
        else:
            pieces.append(_BARE_PERCENT if match[2] == "%" else match[2])
    return pieces


# Every way a piece of a URL can be written: as itself, escaped in either case, or a bare %.
_ESCAPE = st.builds(
    lambda digits: f"%{digits}", st.text(alphabet="0123456789abcdefABCDEF", min_size=2, max_size=2)
)
_PIECES = st.lists(st.one_of(st.sampled_from(list("/?=&+%~-._aAbB0149fFgz")), _ESCAPE))


@given(_PIECES)
def test_dedupe_key_is_idempotent_and_keeps_the_meaning(pieces: list[str]) -> None:
    url = "https://example.com/" + "".join(pieces)
    key = dedupe_key(url)
    assert dedupe_key(key) == key
    # So two URLs with one key mean the same: the key merges nothing else.
    assert meaning(key) == meaning(url)


def write(piece: str | tuple[str], data: st.DataObject) -> str:
    """One random way to write a piece of meaning(url)."""
    if piece == _BARE_PERCENT:
        return "%"
    assert isinstance(piece, str)
    if piece.startswith("%"):  # an escape that must stay one, in either case
        return data.draw(st.sampled_from([piece, piece.lower()]))
    if piece in _UNRESERVED:
        escape = f"%{ord(piece):02X}"
        return data.draw(st.sampled_from([piece, escape, escape.lower()]))
    return piece


@given(_PIECES, st.data())
def test_dedupe_key_is_the_same_for_every_way_of_writing_a_url(
    pieces: list[str], data: st.DataObject
) -> None:
    url = "https://example.com/" + "".join(pieces)
    other = "".join(write(piece, data) for piece in meaning(url))
    # Writing a digit plainly after a bare "%" can make an escape; that's another URL.
    assume(meaning(other) == meaning(url))
    assert dedupe_key(other) == dedupe_key(url)
