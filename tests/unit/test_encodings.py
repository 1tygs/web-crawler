"""The Encoding Standard's labels and decoders, checked against its own data
(tests/fixtures/whatwg/README.md)."""

import json
from pathlib import Path
from typing import Any

import pytest

from web_crawler.encodings import LABELS, UTF_8, decode, get_encoding, sniff_bom

WHATWG = Path(__file__).parents[1] / "fixtures" / "whatwg"
ENCODINGS: list[dict[str, Any]] = [
    encoding
    for group in json.loads((WHATWG / "encodings.json").read_text("utf-8"))
    for encoding in group["encodings"]
]
INDEXES: dict[str, list[int | None]] = json.loads(
    (WHATWG / "single-byte-indexes.json").read_text("utf-8")
)
# ISO-8859-8-I decodes with ISO-8859-8's index; it only orders the text differently.
SINGLE_BYTE = {
    name: "iso-8859-8" if name == "ISO-8859-8-I" else name.lower()
    for name in (encoding["name"] for encoding in ENCODINGS)
    if name.lower() in INDEXES or name == "ISO-8859-8-I"
}


def test_every_label_names_its_encoding() -> None:
    assert {
        label: encoding["name"] for encoding in ENCODINGS for label in encoding["labels"]
    } == LABELS
    assert len(LABELS) == 228
    assert len(SINGLE_BYTE) == 28


@pytest.mark.parametrize(("name", "index"), SINGLE_BYTE.items())
def test_single_byte_encodings_decode_every_byte_as_the_standard_says(
    name: str, index: str
) -> None:
    expected = "".join(map(chr, range(0x80))) + "".join(
        "\ufffd" if code_point is None else chr(code_point) for code_point in INDEXES[index]
    )
    assert decode(bytes(range(256)), name) == expected


MARKUP = b'<a href="/x?y=1&amp;z">text</a>\n'


@pytest.mark.parametrize("name", [encoding["name"] for encoding in ENCODINGS])
def test_every_encoding_decodes(name: str) -> None:
    text = decode(MARKUP, name)
    if name == "replacement":
        assert text == "\ufffd"
    elif name.startswith("UTF-16"):  # two bytes a character
        assert len(text) == len(MARKUP) // 2
    else:  # every other encoding is ASCII-compatible
        assert text == MARKUP.decode("ascii")


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("utf-8", UTF_8),
        (" UTF8\n", UTF_8),
        ("unicode-1-1-utf-8", UTF_8),
        # What Python calls ISO-8859-1 and ASCII, browsers decode as windows-1252.
        ("\tLatin1 ", "windows-1252"),
        ("ascii", "windows-1252"),
        ("ISO-8859-1", "windows-1252"),
        ("iso-8859-9", "windows-1254"),
        ("gb2312", "GBK"),
        ("x-mac-ukrainian", "x-mac-cyrillic"),
        ("Shift_JIS", "Shift_JIS"),
        ("utf-16", "UTF-16LE"),
        ("iso-2022-kr", "replacement"),
    ],
)
def test_get_encoding(label: str, name: str) -> None:
    assert get_encoding(label) == name


@pytest.mark.parametrize(
    "label",
    [
        "",
        "utf-7",
        "latin-1",  # Python's alias, not the standard's
        "\u212aoi8-r",  # KELVIN SIGN, which Unicode lowercases to k
        "utf\u00a0-8",
        "\x0butf-8",  # a vertical tab isn't ASCII whitespace
    ],
)
def test_get_encoding_of_an_unknown_label(label: str) -> None:
    assert get_encoding(label) is None


@pytest.mark.parametrize(
    ("data", "bom"),
    [
        (b"\xef\xbb\xbf<html>", (UTF_8, 3)),
        (b"\xfe\xff\x00<", ("UTF-16BE", 2)),
        (b"\xff\xfe<\x00", ("UTF-16LE", 2)),
        (b"\xef\xbb<html>", None),
        (b"<html>", None),
        (b"", None),
    ],
)
def test_sniff_bom(data: bytes, bom: tuple[str, int] | None) -> None:
    assert sniff_bom(data) == bom


@pytest.mark.parametrize(
    ("data", "name", "text"),
    [
        # A byte order mark overrides the encoding, and is dropped.
        (b"\xef\xbb\xbfcaf\xc3\xa9", "windows-1252", "café"),
        (b"\xff\xfea\x00", UTF_8, "a"),
        (b"\xfe\xff\x00a", "windows-1252", "a"),
        # Undecodable bytes become U+FFFD; a cut-off UTF-16 character too.
        (b"a\xffb", UTF_8, "a\ufffdb"),
        (b"a\xaab", "windows-1253", "a\ufffdb"),
        (b"a\x00b", "UTF-16LE", "a\ufffd"),
        # The bytes Python leaves undefined in windows-1252 are C1 controls to a browser.
        (b"\x80\x81\x8d\x8f\x90\x9d", "windows-1252", "\u20ac\x81\x8d\x8f\x90\x9d"),
        (b"\xae\xbe", "KOI8-U", "\u045e\u040e"),
        (b"\xca", "windows-1255", "\u05ba"),
        (b"A\x80\xff", "x-user-defined", "A\uf780\uf7ff"),
        (b"", "replacement", ""),
        # Multi-byte encodings, each through its Python codec.
        (b"\x93\xfa\x96\x7b", "Shift_JIS", "日本"),
        (b"\xc6\xfc\xcb\xdc", "EUC-JP", "日本"),
        (b"\x1b$BF|K\\\x1b(B", "ISO-2022-JP", "日本"),
        (b"\xd6\xd0\xce\xc4", "GBK", "中文"),
        (b"\x81\x30\x81\x30", "gb18030", "\x80"),
        (b"\xa4\xa4\xa4\xe5", "Big5", "中文"),
        (b"\xc7\xd1\xb1\xb9", "EUC-KR", "한국"),
    ],
)
def test_decode(data: bytes, name: str, text: str) -> None:
    assert decode(data, name) == text


def test_known_differences_from_the_standards_multi_byte_decoders() -> None:
    # Python's codecs differ from the standard on a few hundred rare characters (DESIGN.md
    # A.14). These are pinned so that an upgrade that changes them is noticed.
    assert decode(b"\xa1\xc1", "EUC-JP") == "\u301c"  # the standard: U+FF5E
    assert decode(b"\xa1\x45", "Big5") == "\u2022"  # the standard: U+2027
    assert decode(b"\xa6\xd9", "gb18030") == "\ue78d"  # the standard: U+FE10
