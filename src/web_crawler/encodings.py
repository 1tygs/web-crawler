"""The Encoding Standard's labels and decoders (https://encoding.spec.whatwg.org/).

Python's codec names aren't WHATWG labels: "latin1" and "ascii" mean ISO-8859-1 and ASCII
to Python, but windows-1252 to a browser, and some WHATWG labels mean nothing to Python. So
a page's declared charset goes through get_encoding, which knows every label in the
standard, and decode() turns its bytes into the text a browser would see.

Each single-byte encoding decodes exactly as the standard's index says. Python's codec gives
the same character for every byte but a few: windows-874 and windows-1250 to 1258 leave
some bytes in 0x80 to 0x9F undefined, which WHATWG maps to the C1 control of the same value,
and three bytes differ outright. Multi-byte encodings go through Python's nearest codec,
which differs from the standard on a few hundred rare characters (DESIGN.md A.14).

The label table is the standard's encodings.json, which tests/unit/test_encodings.py checks
it against. The standard is © WHATWG, under CC BY 4.0; as source code, this table is under
the BSD 3-Clause License.
"""

import codecs
import functools

UTF_8 = "UTF-8"
WINDOWS_1252 = "windows-1252"

# Each encoding's name, with its labels.
_ENCODINGS: dict[str, tuple[str, ...]] = {
    # The Encoding
    UTF_8: ("unicode-1-1-utf-8", "unicode11utf8", "unicode20utf8", "utf-8", "utf8",
            "x-unicode20utf8"),
    # Legacy single-byte encodings
    "IBM866": ("866", "cp866", "csibm866", "ibm866"),
    "ISO-8859-2": ("csisolatin2", "iso-8859-2", "iso-ir-101", "iso8859-2", "iso88592",
                   "iso_8859-2", "iso_8859-2:1987", "l2", "latin2"),
    "ISO-8859-3": ("csisolatin3", "iso-8859-3", "iso-ir-109", "iso8859-3", "iso88593",
                   "iso_8859-3", "iso_8859-3:1988", "l3", "latin3"),
    "ISO-8859-4": ("csisolatin4", "iso-8859-4", "iso-ir-110", "iso8859-4", "iso88594",
                   "iso_8859-4", "iso_8859-4:1988", "l4", "latin4"),
    "ISO-8859-5": ("csisolatincyrillic", "cyrillic", "iso-8859-5", "iso-ir-144", "iso8859-5",
                   "iso88595", "iso_8859-5", "iso_8859-5:1988"),
    "ISO-8859-6": ("arabic", "asmo-708", "csiso88596e", "csiso88596i", "csisolatinarabic",
                   "ecma-114", "iso-8859-6", "iso-8859-6-e", "iso-8859-6-i", "iso-ir-127",
                   "iso8859-6", "iso88596", "iso_8859-6", "iso_8859-6:1987"),
    "ISO-8859-7": ("csisolatingreek", "ecma-118", "elot_928", "greek", "greek8", "iso-8859-7",
                   "iso-ir-126", "iso8859-7", "iso88597", "iso_8859-7", "iso_8859-7:1987",
                   "sun_eu_greek"),
    "ISO-8859-8": ("csiso88598e", "csisolatinhebrew", "hebrew", "iso-8859-8", "iso-8859-8-e",
                   "iso-ir-138", "iso8859-8", "iso88598", "iso_8859-8", "iso_8859-8:1988",
                   "visual"),
    "ISO-8859-8-I": ("csiso88598i", "iso-8859-8-i", "logical"),
    "ISO-8859-10": ("csisolatin6", "iso-8859-10", "iso-ir-157", "iso8859-10", "iso885910", "l6",
                    "latin6"),
    "ISO-8859-13": ("iso-8859-13", "iso8859-13", "iso885913"),
    "ISO-8859-14": ("iso-8859-14", "iso8859-14", "iso885914"),
    "ISO-8859-15": ("csisolatin9", "iso-8859-15", "iso8859-15", "iso885915", "iso_8859-15",
                    "l9"),
    "ISO-8859-16": ("iso-8859-16",),
    "KOI8-R": ("cskoi8r", "koi", "koi8", "koi8-r", "koi8_r"),
    "KOI8-U": ("koi8-ru", "koi8-u"),
    "macintosh": ("csmacintosh", "mac", "macintosh", "x-mac-roman"),
    "windows-874": ("dos-874", "iso-8859-11", "iso8859-11", "iso885911", "tis-620",
                    "windows-874"),
    "windows-1250": ("cp1250", "windows-1250", "x-cp1250"),
    "windows-1251": ("cp1251", "windows-1251", "x-cp1251"),
    WINDOWS_1252: ("ansi_x3.4-1968", "ascii", "cp1252", "cp819", "csisolatin1", "ibm819",
                   "iso-8859-1", "iso-ir-100", "iso8859-1", "iso88591", "iso_8859-1",
                   "iso_8859-1:1987", "l1", "latin1", "us-ascii", "windows-1252", "x-cp1252"),
    "windows-1253": ("cp1253", "windows-1253", "x-cp1253"),
    "windows-1254": ("cp1254", "csisolatin5", "iso-8859-9", "iso-ir-148", "iso8859-9",
                     "iso88599", "iso_8859-9", "iso_8859-9:1989", "l5", "latin5",
                     "windows-1254", "x-cp1254"),
    "windows-1255": ("cp1255", "windows-1255", "x-cp1255"),
    "windows-1256": ("cp1256", "windows-1256", "x-cp1256"),
    "windows-1257": ("cp1257", "windows-1257", "x-cp1257"),
    "windows-1258": ("cp1258", "windows-1258", "x-cp1258"),
    "x-mac-cyrillic": ("x-mac-cyrillic", "x-mac-ukrainian"),
    # Legacy multi-byte Chinese (simplified) encodings
    "GBK": ("chinese", "csgb2312", "csiso58gb231280", "gb2312", "gb_2312", "gb_2312-80", "gbk",
            "iso-ir-58", "x-gbk"),
    "gb18030": ("gb18030",),
    # Legacy multi-byte Chinese (traditional) encodings
    "Big5": ("big5", "big5-hkscs", "cn-big5", "csbig5", "x-x-big5"),
    # Legacy multi-byte Japanese encodings
    "EUC-JP": ("cseucpkdfmtjapanese", "euc-jp", "x-euc-jp"),
    "ISO-2022-JP": ("csiso2022jp", "iso-2022-jp"),
    "Shift_JIS": ("csshiftjis", "ms932", "ms_kanji", "shift-jis", "shift_jis", "sjis",
                  "windows-31j", "x-sjis"),
    # Legacy multi-byte Korean encodings
    "EUC-KR": ("cseuckr", "csksc56011987", "euc-kr", "iso-ir-149", "korean", "ks_c_5601-1987",
               "ks_c_5601-1989", "ksc5601", "ksc_5601", "windows-949"),
    # Legacy miscellaneous encodings
    "replacement": ("csiso2022kr", "hz-gb-2312", "iso-2022-cn", "iso-2022-cn-ext",
                    "iso-2022-kr", "replacement"),
    "UTF-16BE": ("unicodefffe", "utf-16be"),
    "UTF-16LE": ("csunicode", "iso-10646-ucs-2", "ucs-2", "unicode", "unicodefeff", "utf-16",
                 "utf-16le"),
    "x-user-defined": ("x-user-defined",),
}  # fmt: skip

LABELS = {label: name for name, labels in _ENCODINGS.items() for label in labels}

# The Python codec behind each single-byte encoding, and the bytes it decodes differently.
_SINGLE_BYTE = {
    "IBM866": "cp866",
    "ISO-8859-2": "iso8859_2",
    "ISO-8859-3": "iso8859_3",
    "ISO-8859-4": "iso8859_4",
    "ISO-8859-5": "iso8859_5",
    "ISO-8859-6": "iso8859_6",
    "ISO-8859-7": "iso8859_7",
    "ISO-8859-8": "iso8859_8",
    "ISO-8859-8-I": "iso8859_8",  # the same characters; it only orders them differently
    "ISO-8859-10": "iso8859_10",
    "ISO-8859-13": "iso8859_13",
    "ISO-8859-14": "iso8859_14",
    "ISO-8859-15": "iso8859_15",
    "ISO-8859-16": "iso8859_16",
    "KOI8-R": "koi8_r",
    "KOI8-U": "koi8_u",
    "macintosh": "mac_roman",
    "windows-874": "cp874",
    "windows-1250": "cp1250",
    "windows-1251": "cp1251",
    WINDOWS_1252: "cp1252",
    "windows-1253": "cp1253",
    "windows-1254": "cp1254",
    "windows-1255": "cp1255",
    "windows-1256": "cp1256",
    "windows-1257": "cp1257",
    "windows-1258": "cp1258",
    "x-mac-cyrillic": "mac_cyrillic",
}
_DIFFERENT_BYTES = {
    # WHATWG's KOI8-U is KOI8-RU: Belarusian ў and Ў where Python's KOI8-U has box drawing.
    "KOI8-U": {0xAE: "\u045e", 0xBE: "\u040e"},
    "windows-1255": {0xCA: "\u05ba"},  # HEBREW POINT HOLAM HASER FOR VAV
}
_MULTI_BYTE = {
    UTF_8: "utf-8",
    "GBK": "gb18030",  # the standard decodes GBK with its gb18030 decoder
    "gb18030": "gb18030",
    "Big5": "big5hkscs",  # the standard's Big5 includes HKSCS
    "EUC-JP": "euc_jp",
    "ISO-2022-JP": "iso2022_jp_ext",  # the variant that reads half-width katakana too
    "Shift_JIS": "cp932",
    "EUC-KR": "cp949",
    "UTF-16BE": "utf-16-be",
    "UTF-16LE": "utf-16-le",
}

_ASCII_WHITESPACE = "\t\n\f\r "
_BOMS = ((b"\xef\xbb\xbf", UTF_8), (b"\xfe\xff", "UTF-16BE"), (b"\xff\xfe", "UTF-16LE"))
_UNDEFINED = "\ufffe"  # marks a byte as undecodable in a codecs.charmap_decode table
_C1_CONTROLS = range(0x80, 0xA0)


def get_encoding(label: str) -> str | None:
    """WHATWG "get an encoding": the name of the encoding that label names, or None."""
    label = label.strip(_ASCII_WHITESPACE)
    # Every label is ASCII, and the match is ASCII case-insensitive: str.lower() would also
    # fold KELVIN SIGN into k.
    return LABELS.get(label.lower()) if label.isascii() else None


def sniff_bom(data: bytes) -> tuple[str, int] | None:
    """WHATWG "BOM sniff": the encoding data's byte order mark selects, and its length."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return encoding, len(bom)
    return None


def decode(data: bytes, encoding: str) -> str:
    """WHATWG "decode": data as text in encoding, a name from get_encoding. A byte order
    mark overrides encoding and is dropped, and undecodable bytes become U+FFFD."""
    if (bom := sniff_bom(data)) is not None:
        encoding, data = bom[0], data[bom[1] :]
    if (codec := _MULTI_BYTE.get(encoding)) is not None:
        return data.decode(codec, "replace")
    if encoding == "replacement":
        # For encodings that aren't safe to decode: a page in one shows a single U+FFFD.
        return "\ufffd" if data else ""
    return codecs.charmap_decode(data, "replace", _table(encoding))[0]


@functools.cache
def _table(encoding: str) -> str:
    """The decoding table of a single-byte encoding, or x-user-defined, for charmap_decode."""
    if encoding == "x-user-defined":
        # ASCII, then the private-use characters U+F780 to U+F7FF for the bytes 0x80 to 0xFF.
        return "".join(map(chr, [*range(0x80), *range(0xF780, 0xF800)]))
    codec = _SINGLE_BYTE[encoding]
    chars = []
    for byte in range(256):
        try:
            chars.append(bytes([byte]).decode(codec))
        except UnicodeDecodeError:
            chars.append(chr(byte) if byte in _C1_CONTROLS else _UNDEFINED)
    for byte, char in _DIFFERENT_BYTES.get(encoding, {}).items():
        chars[byte] = char
    return "".join(chars)
