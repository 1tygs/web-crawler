import functools
import gzip
import random
import struct
import time
import zlib
from collections.abc import Callable
from compression import zstd

import brotli
import pytest
from hypothesis import given
from hypothesis import strategies as st

from web_crawler.content import (
    HTML_TYPES,
    MAX_CODINGS,
    PRESCAN_BYTES,
    SNIFF_BYTES,
    Charset,
    ContentDecodeError,
    Source,
    change_encoding,
    decode_body,
    meta_encoding,
    nosniff,
    parse_content_type,
    parse_mime_type,
    pick_charset,
    prescan,
    sniff_html,
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


def raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def big_window(data: bytes) -> bytes:
    """A zstd frame whose header asks for a 16 MiB window. Streamed, the compressor doesn't
    know the size up front, so it can't shrink the window to fit the data."""
    compressor = zstd.ZstdCompressor(options={zstd.CompressionParameter.window_log: 24})
    return compressor.compress(data) + compressor.flush()


COMPRESS: dict[str, Callable[[bytes], bytes]] = {
    "gzip": gzip.compress,
    "x-gzip": gzip.compress,
    "deflate": zlib.compress,
    "br": functools.partial(brotli.compress, quality=5),
    "zstd": zstd.compress,
}
PAGE = b"<!doctype html>" + b"".join(b'<a href="/%d">%d</a>\n' % (n, n) for n in range(2000))
# 1 MB of text that compresses about as well as a page, without long repeats: a stream's
# blocks then each hold a similar share of it.
TEXT = bytes(random.Random(1).choices(b"abcdefghijklmnopqrstuvwxyz <>/\n", k=1_000_000))


@pytest.mark.parametrize("coding", COMPRESS)
def test_decode_body_undoes_each_coding(coding: str) -> None:
    assert decode_body(COMPRESS[coding](PAGE), coding, 1 << 20) == (PAGE, False)


@pytest.mark.parametrize("content_encoding", [None, "", "identity", " Identity ", "utf-8", "x"])
def test_decode_body_leaves_a_body_without_codings_alone(content_encoding: str | None) -> None:
    # A coding the crawler never asks for is skipped, as Firefox skips it.
    assert decode_body(PAGE, content_encoding, 1 << 20) == (PAGE, False)
    assert decode_body(PAGE, content_encoding, 10) == (PAGE[:10], True)


@pytest.mark.parametrize(
    ("content_encoding", "body"),
    [
        ("GZIP", gzip.compress(PAGE)),
        (" br\t", brotli.compress(PAGE)),
        ("deflate", raw_deflate(PAGE)),  # without zlib's header, as some servers send it
        # Undone last first, as applied: gzip, then br.
        ("gzip, br", brotli.compress(gzip.compress(PAGE))),
        ("zstd,deflate", zlib.compress(zstd.compress(PAGE))),
        ("gzip, identity, utf-8,, br", brotli.compress(gzip.compress(PAGE))),
        # Several gzip members, or zstd frames, in a row; and bytes after the last ignored.
        ("gzip", gzip.compress(PAGE[:100]) + gzip.compress(PAGE[100:])),
        ("gzip", gzip.compress(PAGE) + b"\r\n"),
        ("gzip", gzip.compress(PAGE) + b"\x1f\x8b not a member"),
        ("zstd", zstd.compress(PAGE[:7]) + zstd.compress(PAGE[7:]) + b"\0"),
        ("zstd", zstd.compress(PAGE) + b"\x28\xb5\x2f\xfd not a frame"),
        ("zstd", struct.pack("<II", 0x184D2A5F, 3) + b"abc" + zstd.compress(PAGE)),
        ("deflate", zlib.compress(PAGE) + b"junk"),
        # brotli's decoder rejects bytes after the stream, which Chrome ignores.
        ("br", brotli.compress(PAGE) + b"\n"),
        ("br", brotli.compress(PAGE) + brotli.compress(PAGE)),
    ],
)
def test_decode_body_reads_codings_as_browsers_do(content_encoding: str, body: bytes) -> None:
    assert decode_body(body, content_encoding, 1 << 20) == (PAGE, False)


@pytest.mark.parametrize("coding", COMPRESS)
def test_decode_body_caps_the_output(coding: str) -> None:
    body = COMPRESS[coding](PAGE)
    assert decode_body(body, coding, len(PAGE)) == (PAGE, False)
    assert decode_body(body, coding, len(PAGE) - 1) == (PAGE[:-1], True)
    assert decode_body(body, coding, 1000) == (PAGE[:1000], True)


def test_decode_body_finds_the_end_of_a_brotli_stream_with_bytes_after_it() -> None:
    body = brotli.compress(TEXT, quality=5) + b"trailing"
    assert decode_body(body, "br", 1 << 20) == (TEXT, False)
    assert decode_body(body, "br", 1 << 18) == (TEXT[: 1 << 18], True)
    assert decode_body(body[:-20], "br", 1 << 20)[1]  # cut short, no bytes after it


def test_decode_body_caps_brotlis_soft_limit() -> None:
    # brotli's output_buffer_limit can return twice what it's asked for (plan §2).
    body = brotli.compress(TEXT, quality=5)
    assert len(brotli.Decompressor().process(body, output_buffer_limit=1 << 18)) > 1 << 18
    assert decode_body(body, "br", 1 << 18) == (TEXT[: 1 << 18], True)


BOMBS = {
    "gzip": gzip.compress(bytes(16 << 20), 9),  # 16 MiB of zeros in 16 KiB
    "br": brotli.compress(bytes(16 << 20), quality=5),  # ... in 26 bytes
    "zstd": zstd.compress(bytes(16 << 20)),  # ... in 0.5 KiB
}


@pytest.mark.parametrize("coding", BOMBS)
def test_decode_body_stops_a_bomb_at_the_cap(coding: str) -> None:
    started = time.process_time()
    assert decode_body(BOMBS[coding], coding, 1 << 16) == (bytes(1 << 16), True)
    assert time.process_time() - started < 0.1  # it doesn't decompress the rest


def test_decode_body_caps_every_stage() -> None:
    # gzip, then gzip again: the outer layer alone decompresses to more than the cap.
    double = gzip.compress(BOMBS["gzip"])
    output, truncated = decode_body(double, "gzip, gzip", 1 << 15)
    assert truncated
    assert output == bytes(len(output))
    assert 0 < len(output) <= 1 << 15


@pytest.mark.parametrize("coding", COMPRESS)
def test_decode_body_of_a_stream_cut_short_is_what_it_decodes_to(coding: str) -> None:
    # As a browser renders a page whose body ended early. zstd decodes whole blocks of up
    # to 128 KiB, so the text is longer than that.
    body = COMPRESS[coding](TEXT)
    output, truncated = decode_body(body[: len(body) // 2], coding, 1 << 20)
    assert truncated
    assert 0 < len(output) < len(TEXT)
    assert TEXT.startswith(output)


@pytest.mark.parametrize("coding", COMPRESS)
def test_decode_body_of_an_empty_body(coding: str) -> None:
    assert decode_body(b"", coding, 1 << 20) == (b"", False)


def test_decode_body_of_one_byte_of_deflate() -> None:
    # Too short for zlib's header, so it's read as raw deflate: a stream cut short.
    assert decode_body(zlib.compress(PAGE)[:1], "deflate", 1 << 20) == (b"", True)


@pytest.mark.parametrize(
    ("content_encoding", "body", "message"),
    [
        ("gzip", b"<html>", "gzip: Error -3 while decompressing data: incorrect header check"),
        ("gzip", gzip.compress(PAGE)[:-8] + b"\0" * 8, "gzip: Error -3 while decompressing"),
        ("deflate", b"\xff" * 20, "deflate: Error -3 while decompressing data"),
        ("br", b"<html>", "br: brotli: decoder failed"),
        ("br", b"<html>" * 10_000, "br: brotli: decoder failed"),
        ("zstd", b"\x28\xb5\x2f\xfd" + b"\xff" * 20, "zstd: "),
        # A frame that asks for a window over 8 MB, which RFC 9659 lets a decoder refuse.
        (
            "zstd",
            big_window(PAGE),
            "zstd: Unable to decompress Zstandard data: Frame requires too much memory",
        ),
        ("gzip, br", gzip.compress(PAGE), "br: brotli: decoder failed"),
        (", ".join(["gzip"] * (MAX_CODINGS + 1)), b"x", "17 content codings; at most 16 count"),
    ],
)
def test_decode_body_rejects_corrupt_data(content_encoding: str, body: bytes, message: str) -> None:
    with pytest.raises(ContentDecodeError) as error:
        decode_body(body, content_encoding, 1 << 20)
    assert str(error.value).startswith(message)


def test_decode_body_undoes_up_to_16_codings() -> None:
    body = PAGE
    for _ in range(MAX_CODINGS):
        body = gzip.compress(body)
    assert decode_body(body, ", ".join(["gzip"] * MAX_CODINGS), 1 << 20) == (PAGE, False)


@given(
    data=st.binary(max_size=3000),
    codings=st.lists(st.sampled_from(sorted(COMPRESS)), max_size=3),
    cap=st.integers(min_value=1, max_value=4000),
)
def test_decode_body_gives_a_prefix_of_what_was_encoded(
    data: bytes, codings: list[str], cap: int
) -> None:
    body = data
    for coding in codings:
        body = COMPRESS[coding](body)
    output, truncated = decode_body(body, ", ".join(codings), cap)
    assert data.startswith(output)
    assert len(output) <= cap
    assert truncated or output == data
    # With room for every stage, which can be a little bigger than the data, it's exact.
    if cap >= len(data) + 100 * len(codings):
        assert (output, truncated) == (data, False)


@pytest.mark.parametrize(
    "header",
    [
        b"<!DOCTYPE HTML>",
        b"<!doctype html>",
        b"<!DocType Html PUBLIC>",
        b"<html>",
        b"<HTML lang=en>",
        b"<head>",
        b"<script>",
        b"<iframe ",
        b"<h1>",
        b"<div class=x>",
        b"<font>",
        b"<table>",
        b"<a href=/x>",
        b"<style>",
        b"<title>",
        b"<b>",
        b"<body>",
        b"<br>",
        b"<p>",
        b"<!-- a comment -->",
        b"<!-->",
        b" \t\n\x0c\r<html>",  # leading whitespace is skipped
        b"<html>" + b"x" * SNIFF_BYTES,
    ],
)
def test_sniffs_html(header: bytes) -> None:
    assert sniff_html(header)


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"   ",
        b"<html",  # nothing after the pattern
        b"<htmlx>",
        b"<html\n>",  # a tag ends at a space or ">" only
        b"<abbr>",
        b"<!---->",
        b"<!DOCTYPE  HTML>",
        b"<!DOCTYPEHTML>",
        b"<?xml version='1.0'?><html>",
        b"%PDF-1.7",
        b"\xef\xbb\xbf<html>",  # a byte order mark isn't whitespace
        b"\x0b<html>",  # nor is a vertical tab
        b"hello <html>",
        b"<h2>",
        b"<span>",
    ],
)
def test_doesnt_sniff_html(header: bytes) -> None:
    assert not sniff_html(header)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("nosniff", True),
        ("NoSniff", True),
        (" nosniff\t", True),
        ("nosniff, other", True),
        ("other, nosniff", False),  # only the first value counts
        ('"nosniff"', False),
        ("", False),
        ("no-sniff", False),
    ],
)
def test_nosniff(value: str | None, expected: bool) -> None:
    assert nosniff(value) is expected
