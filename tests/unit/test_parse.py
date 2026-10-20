import html

import pytest
from hypothesis import given
from hypothesis import strategies as st
from selectolax.lexbor import LexborHTMLParser

from web_crawler.parse import parse_page, parse_refresh, parse_srcset
from web_crawler.types import ParseJob, WorkerContext

PAGE = "https://example.com/dir/page.html"
CTX = WorkerContext(scope_prefixes=("https://example.com/", "http://example.com/"))


def links(markup: str, url: str = PAGE) -> list[str]:
    """The links parse_page prints for markup, after checking the record's shape."""
    lines = parse_page(ParseJob(url=url, body=markup.encode()), CTX).record.decode().split("\n")
    assert lines[0] == url
    assert lines[-2:] == ["", ""]  # each record ends with a blank line
    assert all(line.startswith("  ") for line in lines[1:-2])
    return [line.removeprefix("  ") for line in lines[1:-2]]


def anchors(*hrefs: str) -> str:
    return "".join(f'<a href="{html.escape(href)}">x</a>' for href in hrefs)


def test_renders_the_text_record() -> None:
    result = parse_page(
        ParseJob(url=PAGE, body=b'<a href="/about">a</a><a href="https://github.com/x">g</a>'),
        CTX,
    )
    assert result.record == (
        b"https://example.com/dir/page.html\n"
        b"  https://example.com/about\n"
        b"  https://github.com/x\n"
        b"\n"
    )
    assert result.links == [("https://example.com/about", "https://example.com/about")]
    assert result.links_found == 2


def test_a_page_without_links() -> None:
    result = parse_page(ParseJob(url=PAGE, body=b"<p>nothing here</p>"), CTX)
    assert result == (b"https://example.com/dir/page.html\n\n", [], 0)


def test_finds_navigational_links_in_document_order() -> None:
    markup = '<iframe src="/1"></iframe><a href="/2">x</a><map><area href="/3"></map><a href="/4">'
    assert links(markup) == [f"https://example.com/{n}" for n in "1234"]
    frameset = '<frameset><frame src="/top"><frame src="/main"></frameset>'
    assert links(frameset) == ["https://example.com/top", "https://example.com/main"]


@pytest.mark.parametrize(
    ("href", "url"),
    [
        ("other.html", "https://example.com/dir/other.html"),
        ("../up", "https://example.com/up"),
        ("./a/./b/../c", "https://example.com/dir/a/c"),
        ("?q=1", "https://example.com/dir/page.html?q=1"),
        ("", "https://example.com/dir/page.html"),
        ("//example.com/x", "https://example.com/x"),
        ("HTTP://Example.COM:80/A", "http://example.com/A"),
        ("https://example.com:443/", "https://example.com/"),
        ("  /path with space  ", "https://example.com/path%20with%20space"),
        ("/a\n/b\t/c", "https://example.com/a/b/c"),
        ("\\about", "https://example.com/about"),
        ("/café?q=é", "https://example.com/caf%C3%A9?q=%C3%A9"),
        ("https://bücher.example/straße", "https://xn--bcher-kva.example/stra%C3%9Fe"),
        ("/a?b=%2f&c=%2F", "https://example.com/a?b=%2f&c=%2F"),
    ],
)
def test_resolves_links_as_a_browser_does(href: str, url: str) -> None:
    assert links(anchors(href)) == [url]


def test_resolves_entities_in_attribute_values() -> None:
    assert links('<a href="/x?a=1&amp;b=2&#x26;c=3">x</a>') == ["https://example.com/x?a=1&b=2&c=3"]


def test_resolves_against_the_url_it_was_fetched_from() -> None:
    # After a redirect, the final URL is the base, not the one that was requested.
    assert links(anchors("next"), url="https://example.com/new/") == [
        "https://example.com/new/next"
    ]


@pytest.mark.parametrize(
    "href",
    ["mailto:a@example.com", "javascript:void(0)", "tel:+1", "data:text/html,x", "ftp://x/"],
)
def test_keeps_http_and_https_only(href: str) -> None:
    assert links(anchors(href, "http://example.com/", "https://example.com/")) == [
        "http://example.com/",
        "https://example.com/",
    ]


def test_strips_fragments_and_keeps_the_first_of_each_url() -> None:
    markup = anchors("#top", "/x#one", "/y", "/x#two", "/x", "https://example.com/dir/page.html")
    assert links(markup) == [PAGE, "https://example.com/x", "https://example.com/y"]


@pytest.mark.parametrize(
    "href", ["http://exa mple.com/", "https://[::1", "http://example.com:99999/"]
)
def test_skips_values_that_dont_parse(href: str) -> None:
    result = parse_page(ParseJob(url=PAGE, body=anchors(href, "/ok").encode()), CTX)
    assert result.links_found == 1
    assert result.links == [("https://example.com/ok", "https://example.com/ok")]


def test_returns_in_scope_links_only_but_prints_all() -> None:
    markup = anchors(
        "/a",
        "http://example.com/b",  # the other scheme, on the default port
        "https://www.example.com/c",  # another host: a subdomain
        "https://example.com:8443/d",  # another port
        "https://elsewhere.org/e",
    )
    result = parse_page(ParseJob(url=PAGE, body=markup.encode()), CTX)
    assert result.links_found == 5
    assert [url for _, url in result.links] == ["https://example.com/a", "http://example.com/b"]
    assert all(key == url for key, url in result.links)


def test_prints_each_spelling_but_crawls_each_resource_once() -> None:
    # Escapes of unreserved characters and the case of other escapes don't change the
    # resource (DESIGN.md §5.6), so the first spelling found is the one to crawl.
    markup = anchors("/~a", "/%7Ea", "/%7ea", "/b%2fc", "/b%2Fc", "/b/c")
    result = parse_page(ParseJob(url=PAGE, body=markup.encode()), CTX)
    assert result.links_found == 6
    assert result.links == [
        ("https://example.com/~a", "https://example.com/~a"),
        ("https://example.com/b%2Fc", "https://example.com/b%2fc"),
        ("https://example.com/b/c", "https://example.com/b/c"),
    ]


@pytest.mark.parametrize(
    ("body", "http_charset", "url"),
    [
        # Undeclared and not UTF-8: windows-1252, so 0xE9 is é.
        (b'<a href="/caf\xe9">x</a>', None, "https://example.com/caf%C3%A9"),
        (b'<a href="/caf\xc3\xa9">x</a>', None, "https://example.com/caf%C3%A9"),
        # Declared by the header, which a <meta> can't overrule, or by a <meta>.
        (b'<a href="/\xe0">x</a>', "windows-1251", "https://example.com/%D0%B0"),
        (
            b'<meta charset=koi8-r><a href="/\xe0">x</a>',
            "windows-1251",
            "https://example.com/%D0%B0",
        ),
        (b'<meta charset=koi8-r><a href="/\xc1">x</a>', None, "https://example.com/%D0%B0"),
        (
            b'<meta charset=shift_jis><a href="/\x93\xfa">x</a>',
            None,
            "https://example.com/%E6%97%A5",
        ),
        # A <meta> beyond the first 1,024 bytes: the parser reaches it and starts over.
        (
            b"<!--" + b"-" * 1024 + b'--><meta charset=windows-1251><a href="/\xe0">x</a>',
            None,
            "https://example.com/%D0%B0",
        ),
        # A byte order mark wins, and isn't text.
        (
            b"\xff\xfe<\x00a\x00 \x00h\x00r\x00e\x00f\x00=\x00/\x00\xe9\x00>\x00",
            None,
            "https://example.com/%C3%A9",
        ),
    ],
)
def test_decodes_a_page_as_a_browser_does(body: bytes, http_charset: str | None, url: str) -> None:
    job = ParseJob(url=PAGE, body=body, http_charset=http_charset)
    assert parse_page(job, CTX).record.decode().split("\n")[1:-2] == [f"  {url}"]


def test_a_byte_order_mark_is_not_text() -> None:
    body = b'\xef\xbb\xbf<!doctype html><frameset><frame src="/top"></frameset>'
    # lexbor reads the mark as text, which ends <head> early and makes it ignore the
    # <frameset>, so parse_page drops it first.
    assert LexborHTMLParser(body).css("frame") == []
    result = parse_page(ParseJob(url=PAGE, body=body), CTX)
    assert result.links == [("https://example.com/top", "https://example.com/top")]


def test_a_page_in_the_replacement_encoding_has_no_links() -> None:
    # Some encodings aren't safe to decode; a browser shows such a page as one U+FFFD.
    job = ParseJob(url=PAGE, body=b'<a href="/x">x</a>', http_charset="iso-2022-kr")
    assert parse_page(job, CTX) == (b"https://example.com/dir/page.html\n\n", [], 0)


def test_a_truncated_page_may_end_inside_a_utf8_character() -> None:
    body = '<a href="/caf\u00e9">x</a><p>\u65e5'.encode()[:-1]
    for truncated in (True, False):
        result = parse_page(ParseJob(url=PAGE, body=body, truncated=truncated), CTX)
        url = (
            "https://example.com/caf%C3%A9" if truncated else "https://example.com/caf%C3%83%C2%A9"
        )
        assert result.links == [(url, url)]


def test_reads_valueless_hrefs_and_svg_xlink() -> None:
    # lexbor reports both as None: `<a href>` has no value, and an SVG <a> matched by [href]
    # has only xlink:href. A valueless href is "", a link to the page itself.
    markup = '<a href>x</a><svg><a xlink:href="/svg">s</a></svg><a href="/ok">ok</a>'
    assert links(markup) == [PAGE, "https://example.com/svg", "https://example.com/ok"]


@pytest.mark.parametrize(
    ("content", "refresh"),
    [
        # A time, then optionally the URL, after ";" or "," or whitespace, and "url=".
        ("0", (True, None)),
        ("  30  ", (True, None)),
        ("0;", (True, None)),
        ("0; url=/next", (True, "/next")),
        ("0;URL=/next", (True, "/next")),
        ("0, url = /next", (True, "/next")),
        ("0 url=/next", (True, "/next")),
        ("0;/next", (True, "/next")),
        ("1.5; url=/next", (True, "/next")),  # the fraction is ignored
        (".5;url=/next", (True, "/next")),
        ("5.;url=/next", (True, "/next")),
        # Quotes around the URL are dropped, up to the matching one.
        ("0; url='/next'", (True, "/next")),
        ('0; url="/next"', (True, "/next")),
        ("0; url='/next", (True, "/next")),
        ("0;'/next' trailing", (True, "/next")),
        ('0; url="/a\'b"', (True, "/a'b")),
        # Without "url=", whatever follows is the URL, "ur" and all.
        ("0; urn:x", (True, "urn:x")),
        ("0; u", (True, "u")),
        ("0; url /next", (True, "url /next")),
        ("0; url=", (True, "")),
        ("0; UrL\t=\t/next", (True, "/next")),
        # Not a refresh at all: no time, or something else right after it.
        ("", (False, None)),
        ("; url=/next", (False, None)),
        ("url=/next", (False, None)),
        ("-1; url=/next", (False, None)),
        ("5x; url=/next", (False, None)),
        ("\u00a05; url=/next", (False, None)),  # NO-BREAK SPACE isn't ASCII whitespace
    ],
)
def test_parse_refresh(content: str, refresh: tuple[bool, str | None]) -> None:
    assert parse_refresh(content) == refresh


@pytest.mark.parametrize(
    ("srcset", "urls"),
    [
        ("a.png", ["a.png"]),
        ("a.png 1x, b.png 2x", ["a.png", "b.png"]),
        ("a.png 100w, b.png 200w 50h", ["a.png", "b.png"]),
        (" a.png  1.5x ,b.png 2e0x", ["a.png", "b.png"]),
        ("a.png,b.png 2x", ["a.png,b.png"]),  # a comma inside a URL stays in it
        ("a.png, b.png,", ["a.png", "b.png"]),
        ("a.png,, ,b.png", ["a.png", "b.png"]),
        ("data:image/gif;base64,R0lG, b.png 2x", ["data:image/gif;base64,R0lG", "b.png"]),
        ("a.png (x, y), b.png", ["b.png"]),  # a parenthesized descriptor is unknown
        # Invalid descriptors drop their candidate.
        ("a.png 2y, b.png", ["b.png"]),
        ("a.png 1X", []),
        ("a.png 0w", []),
        ("a.png 100w 0h", []),
        ("a.png 50h", []),  # a height needs a width
        ("a.png 100w 2x", []),
        ("a.png 2x 2x", []),
        ("a.png 2x 50h", []),
        ("a.png -1x", []),
        ("a.png 1.x", []),
        ("a.png -0x", ["a.png"]),
        ("", []),
        (" , ", []),
    ],
)
def test_parse_srcset(srcset: str, urls: list[str]) -> None:
    assert parse_srcset(srcset) == urls


@pytest.mark.parametrize(
    ("host", "can_host"),
    [
        *[(tag, True) for tag in ("article", "body", "div", "h1", "main", "p", "section", "span")],
        *[(tag, False) for tag in ("a", "ul", "li", "table", "head", "button", "my_element")],
        # A custom element name: a lowercase letter, then a "-" somewhere, from a set of
        # characters that includes most non-ASCII ones, and not one of eight reserved names.
        ("my-element", True),
        ("x-\u00e9l\u00e8ve", True),
        ("a-\U0001f600", True),
        ("my:el-x", False),
        ("x-\u00d7", False),
        ("x-\u2000", False),
        ("font-face", False),
        ("annotation-xml", False),
    ],
)
def test_declarative_shadow_roots_attach_only_to_valid_hosts(host: str, can_host: bool) -> None:
    markup = f'<{host}><template shadowrootmode="open"><a href="/shadow">s</a></template></{host}>'
    assert ("https://example.com/shadow" in links(markup)) is can_host


def test_parses_noscript_as_markup_and_skips_inert_templates() -> None:
    markup = '<noscript><a href="/ns">n</a></noscript><template><a href="/inert">i</a></template>'
    assert links(markup) == ["https://example.com/ns"]


@given(st.lists(st.text(max_size=30), max_size=8))
def test_printed_links_are_unique_http_urls_without_fragments(hrefs: list[str]) -> None:
    printed = links(anchors(*hrefs))
    assert len(printed) == len(set(printed))
    assert all(url.startswith(("http://", "https://")) and "#" not in url for url in printed)
