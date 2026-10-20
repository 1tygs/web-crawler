import pytest

from web_crawler.content import HTML_TYPES, parse_content_type, parse_mime_type


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, (None, None)),
        ("text/html", ("text/html", None)),
        ("application/xhtml+xml", ("application/xhtml+xml", None)),
        # The essence is lowercased; parameter values aren't.
        ("TEXT/HTML; Charset=UTF-8", ("text/html", "UTF-8")),
        ("text/html ; charset=utf-8", ("text/html", "utf-8")),
        (" text/html;charset=utf-8 \t", ("text/html", "utf-8")),
        ('text/html;charset="utf-8"', ("text/html", "utf-8")),
        ('text/html;charset="utf-8" trailing junk', ("text/html", "utf-8")),
        ('text/html;charset="u\\"8"', ("text/html", 'u"8')),
        ('text/html;charset="unterminated', ("text/html", "unterminated")),
        ("text/html;charset=", ("text/html", None)),
        ("text/html;charset= ;charset=gbk", ("text/html", "gbk")),  # an empty value is skipped
        ('text/html;charset="gbk\\', ("text/html", "gbk\\")),  # a backslash at the very end
        ("text/html;charset", ("text/html", None)),
        ("text/html;;charset=utf-8", ("text/html", "utf-8")),
        ("text/html;charset=utf-8;charset=gbk", ("text/html", "utf-8")),  # the first one wins
        ("text/html;foo=bar;charset=gbk", ("text/html", "gbk")),
        ("text/html;charset=ü", ("text/html", "ü")),  # U+0080 to U+00FF are allowed
        ("text/html;charset=€", ("text/html", None)),  # anything above isn't
        ("text/html;ch@rset=utf-8", ("text/html", None)),  # not a token
        # Not a MIME type at all.
        ("", (None, None)),
        ("texthtml", (None, None)),
        ("text/", (None, None)),
        ("/html", (None, None)),
        ("text /html", (None, None)),
        ("te(x)t/html", (None, None)),
        # Two Content-Type headers, combined with a comma: the last valid one counts.
        ("text/html, text/plain", ("text/plain", None)),
        ("text/plain, text/html", ("text/html", None)),
        ("text/html, */*", ("text/html", None)),
        ("text/html, nonsense", ("text/html", None)),
        ('text/plain;x="a, b", text/html', ("text/html", None)),
        ('text/html;x="a, b"', ("text/html", None)),
        # A charset carries over while the essence stays the same, and resets when it changes.
        ("text/html;charset=gbk, text/html", ("text/html", "gbk")),
        ("text/html;charset=gbk, text/plain", ("text/plain", None)),
        ("text/html;charset=gbk, text/html;charset=utf-8", ("text/html", "utf-8")),
    ],
)
def test_parse_content_type(value: str | None, expected: tuple[str | None, str | None]) -> None:
    assert parse_content_type(value) == expected


def test_parse_mime_type_keeps_every_valid_parameter() -> None:
    assert parse_mime_type('Text/HTML; Q=1; level="2"; charset=x') == (
        "text/html",
        {"q": "1", "level": "2", "charset": "x"},
    )


def test_html_types() -> None:
    assert frozenset({"text/html", "application/xhtml+xml"}) == HTML_TYPES
