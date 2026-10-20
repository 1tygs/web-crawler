import pytest

from web_crawler.content import (
    HTML_TYPES,
    PRESCAN_BYTES,
    Charset,
    Source,
    change_encoding,
    meta_encoding,
    parse_content_type,
    parse_mime_type,
    pick_charset,
    prescan,
)
from web_crawler.encodings import UTF_8, WINDOWS_1252


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


@pytest.mark.parametrize(
    ("body", "http_charset", "charset"),
    [
        # A byte order mark beats everything else.
        (b"\xef\xbb\xbf<meta charset=koi8-r>", "windows-1251", Charset(UTF_8, Source.BOM)),
        (b"\xfe\xff\x00<", UTF_8, Charset("UTF-16BE", Source.BOM)),
        # Then the header's charset, if it names an encoding; it can be UTF-16.
        (b"<meta charset=koi8-r>", "ISO-8859-2", Charset("ISO-8859-2", Source.HEADER)),
        (b"<meta charset=koi8-r>", " Latin1 ", Charset(WINDOWS_1252, Source.HEADER)),
        (b"<p>x", "utf-16", Charset("UTF-16LE", Source.HEADER)),
        (b"<meta charset=koi8-r>", "x-unknown", Charset("KOI8-R", Source.META)),
        (b"<meta charset=koi8-r>", "", Charset("KOI8-R", Source.META)),
        # Then a <meta> in the first 1,024 bytes.
        (b'<meta charset="iso-8859-2">', None, Charset("ISO-8859-2", Source.META)),
        (b" " * PRESCAN_BYTES + b"<meta charset=koi8-r>", None, Charset(UTF_8, Source.GUESS)),
        # Then a guess: UTF-8 if the bytes are UTF-8, and windows-1252 otherwise.
        (b"<p>plain ASCII", None, Charset(UTF_8, Source.GUESS)),
        (b"<p>caf\xc3\xa9", None, Charset(UTF_8, Source.GUESS)),
        (b"<p>caf\xc3\xa9 \xff", None, Charset(WINDOWS_1252, Source.GUESS)),
        (b"<p>caf\xe9", None, Charset(WINDOWS_1252, Source.GUESS)),
    ],
)
def test_pick_charset(body: bytes, http_charset: str | None, charset: Charset) -> None:
    assert pick_charset(body, http_charset) == charset


@pytest.mark.parametrize(
    ("body", "encoding"),
    [
        # A body cut at the size cap can end inside a UTF-8 character.
        (b"<p>caf\xc3", UTF_8),
        (b"<p>\xe6\x97", UTF_8),
        (b"<p>caf\xe9", UTF_8),  # which this could be, cut off
        (b"<p>caf\xe9!", WINDOWS_1252),
        (b"<p>\xc3\xa9\xff", WINDOWS_1252),
    ],
)
def test_pick_charset_of_a_truncated_body(body: bytes, encoding: str) -> None:
    assert pick_charset(body, None, truncated=True) == Charset(encoding, Source.GUESS)
    if encoding == UTF_8 and not body.endswith(b"\xc3\xa9"):
        # The same bytes, complete, end in an invalid sequence.
        assert pick_charset(body, None) == Charset(WINDOWS_1252, Source.GUESS)


def test_only_a_declaration_a_parser_can_overrule_is_tentative() -> None:
    assert [Charset(UTF_8, source).tentative for source in Source] == [
        False,  # BOM
        False,  # header
        True,  # meta
        True,  # guess
    ]


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        (b"<meta charset=utf-8>", UTF_8),
        (b'<!DOCTYPE html><html><head><META CHARSET="KOI8-R">', "KOI8-R"),
        (b"<meta/charset=koi8-r>", "KOI8-R"),
        (b"<meta\ncharset = 'koi8-r' >", "KOI8-R"),
        (b"<meta charset=' koi8-r '>", "KOI8-R"),
        # http-equiv="Content-Type" and content, in either order.
        (b'<meta http-equiv="Content-Type" content="text/html; charset=koi8-r">', "KOI8-R"),
        (b"<meta content='text/html;charset=\"koi8-r\"' http-equiv=content-type>", "KOI8-R"),
        (b'<meta http-equiv=content-type content="charset =koi8-r;x">', "KOI8-R"),
        (b'<meta http-equiv=content-type content="charsetcharset=koi8-r">', "KOI8-R"),
        (b'<meta content="text/html; charset=koi8-r">', None),  # no http-equiv
        (b'<meta http-equiv=refresh content="charset=koi8-r">', None),
        (b'<meta http-equiv=content-type content="charset=\'koi8-r">', None),
        (b'<meta http-equiv=content-type content="charset=">', None),
        # A charset attribute wins; one that names nothing blocks the content attribute.
        (
            b'<meta http-equiv=content-type content="charset=koi8-r" charset=iso-8859-2>',
            "ISO-8859-2",
        ),
        (b'<meta charset=bogus http-equiv=content-type content="charset=koi8-r">', None),
        (b"<meta charset=koi8-r charset=iso-8859-2>", "KOI8-R"),  # the first of each name
        (b"<meta charset=utf-8/>", None),  # an unquoted value runs to the space or >
        (b"<meta charset=bogus><meta charset=koi8-r>", "KOI8-R"),
        # A page that can declare itself in ASCII isn't UTF-16, and x-user-defined is
        # windows-1252.
        (b"<meta charset=utf-16le>", UTF_8),
        (b"<meta charset=x-user-defined>", WINDOWS_1252),
        # Comments and other tags' attributes hide what's in them.
        (b"<!-- <meta charset=koi8-r> --><meta charset=iso-8859-2>", "ISO-8859-2"),
        (b"<!--><meta charset=koi8-r>", "KOI8-R"),
        (b"<!---><meta charset=koi8-r>", "KOI8-R"),
        (b'<div title="<meta charset=koi8-r>"><meta charset=iso-8859-2>', "ISO-8859-2"),
        (b"</div x='<meta charset=koi8-r>'><meta charset=iso-8859-2>", "ISO-8859-2"),
        (b"<x <meta charset=koi8-r>", None),  # the meta is x's attributes
        (b"<meta <meta charset=koi8-r>", "KOI8-R"),
        (b"<metacharset=koi8-r>", None),
        (b"<!doctype html><?xml encoding='koi8-r'?><meta charset=iso-8859-2>", "ISO-8859-2"),
        (b"< meta charset=koi8-r><meta charset=iso-8859-2>", "ISO-8859-2"),
        # A tag that the data ends inside doesn't count.
        (b"<meta charset=koi8-r", None),
        (b'<meta charset="koi8-r"', None),
        (b"<meta charset=", None),
        (b"<meta charset", None),
        (b"<!-- <meta charset=koi8-r>", None),
        (b"<div title='x><meta charset=koi8-r>", None),
        (b"<! <meta charset=koi8-r>", None),
        # An XML declaration in UTF-16 without a byte order mark.
        (b"<\x00?\x00x\x00m\x00l\x00", "UTF-16LE"),
        (b"\x00<\x00?\x00x\x00m\x00l", "UTF-16BE"),
        (b"", None),
        (b"<p>no declaration</p>", None),
    ],
)
def test_prescan(data: bytes, encoding: str | None) -> None:
    assert prescan(data) == encoding


def test_prescan_looks_at_the_first_1024_bytes() -> None:
    meta = b"<meta charset=koi8-r>"
    assert prescan((b" " * (PRESCAN_BYTES - len(meta)) + meta)[:PRESCAN_BYTES]) == "KOI8-R"
    assert prescan((b" " * (PRESCAN_BYTES - len(meta) + 1) + meta)[:PRESCAN_BYTES]) is None


@pytest.mark.parametrize(
    ("charset", "http_equiv", "content", "encoding"),
    [
        ("koi8-r", None, None, "KOI8-R"),
        (" KOI8-R ", None, None, "KOI8-R"),
        ("bogus", "Content-Type", "text/html; CHARSET=KOI8-R", "KOI8-R"),
        (None, "content-TYPE", "text/html; charset='koi8-r'", "KOI8-R"),
        (None, "Content-Type", None, None),
        (None, None, "text/html; charset=koi8-r", None),
        (None, "refresh", "charset=koi8-r", None),
        (None, "Content-Type ", "charset=koi8-r", None),
        ("", "Content-Type", "text/html", None),
    ],
)
def test_meta_encoding(
    charset: str | None, http_equiv: str | None, content: str | None, encoding: str | None
) -> None:
    assert meta_encoding(charset, http_equiv, content) == encoding


@pytest.mark.parametrize(
    ("current", "declared", "new"),
    [
        (UTF_8, WINDOWS_1252, WINDOWS_1252),
        (WINDOWS_1252, "KOI8-R", "KOI8-R"),
        (WINDOWS_1252, WINDOWS_1252, None),
        (WINDOWS_1252, "UTF-16LE", UTF_8),
        (UTF_8, "UTF-16BE", None),
        (UTF_8, "x-user-defined", WINDOWS_1252),
        ("UTF-16LE", WINDOWS_1252, None),  # a page that really is UTF-16 stays so
    ],
)
def test_change_encoding(current: str, declared: str, new: str | None) -> None:
    assert change_encoding(current, declared) == new
