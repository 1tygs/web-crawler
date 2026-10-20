"""Appendix A.14: the crawler's legacy decoders against the Encoding Standard's indexes.

Usage: uv run python bench/encoding_check.py INDEXES_JSON

INDEXES_JSON is the standard's indexes.json, e.g. from
https://github.com/whatwg/encoding/blob/main/indexes.json. Single-byte encodings are
compared byte by byte with web_crawler.encodings.decode, and with Python's own codec. Each
multi-byte encoding is compared on every sequence its lead and trail bytes allow, decoded
the way the standard's decoder does.
"""
import json
import sys

from web_crawler.encodings import _MULTI_BYTE, _SINGLE_BYTE, decode

idx = json.load(open(sys.argv[1], encoding="utf-8"))
jis0208, jis0212 = idx["jis0208"], idx["jis0212"]


def lookup(index, pointer):
    code_point = index[pointer] if 0 <= pointer < len(index) else None
    return None if code_point is None else chr(code_point)


def shift_jis(seq):
    lead, byte = seq
    if not (0x40 <= byte <= 0x7E or 0x80 <= byte <= 0xFC):
        return None
    pointer = (lead - (0x81 if lead < 0xA0 else 0xC1)) * 188 + byte - (0x40 if byte < 0x7F else 0x41)
    return chr(0xE000 - 8836 + pointer) if 8836 <= pointer <= 10715 else lookup(jis0208, pointer)


def euc_kr(seq):
    return lookup(idx["euc-kr"], (seq[0] - 0x81) * 190 + seq[1] - 0x41) if 0x41 <= seq[1] <= 0xFE else None


BIG5_PAIRS = {1133: "\u00ca\u0304", 1135: "\u00ca\u030c", 1164: "\u00ea\u0304", 1166: "\u00ea\u030c"}


def big5(seq):
    lead, byte = seq
    if not (0x40 <= byte <= 0x7E or 0xA1 <= byte <= 0xFE):
        return None
    pointer = (lead - 0x81) * 157 + byte - (0x40 if byte < 0x7F else 0x62)
    return BIG5_PAIRS.get(pointer) or lookup(idx["big5"], pointer)


def gb18030(seq):
    lead, byte = seq
    if not (0x40 <= byte <= 0x7E or 0x80 <= byte <= 0xFE):
        return None
    return lookup(idx["gb18030"], (lead - 0x81) * 190 + byte - (0x40 if byte < 0x7F else 0x41))


def euc_jp(seq):
    if seq[0] == 0x8E:
        return chr(0xFF61 - 0xA1 + seq[1]) if 0xA1 <= seq[1] <= 0xDF else None
    if seq[0] == 0x8F:
        return lookup(jis0212, (seq[1] - 0xA1) * 94 + seq[2] - 0xA1)
    return lookup(jis0208, (seq[0] - 0xA1) * 94 + seq[1] - 0xA1)


def iso_2022_jp(seq):
    return lookup(jis0208, (seq[3] - 0x21) * 94 + seq[4] - 0x21)


def pairs(leads, trails, prefix=b"", suffix=b""):
    return [prefix + bytes([lead, trail]) + suffix for lead in leads for trail in trails]


MULTI_BYTE = {
    "Shift_JIS": (shift_jis, pairs([*range(0x81, 0xA0), *range(0xE0, 0xFD)], range(0x40, 0xFD))),
    "EUC-KR": (euc_kr, pairs(range(0x81, 0xFF), range(0x41, 0xFF))),
    "Big5": (big5, pairs(range(0x81, 0xFF), [*range(0x40, 0x7F), *range(0xA1, 0xFF)])),
    "gb18030": (gb18030, pairs(range(0x81, 0xFF), [*range(0x40, 0x7F), *range(0x80, 0xFF)])),
    "EUC-JP": (euc_jp, pairs(range(0xA1, 0xFF), range(0xA1, 0xFF)) + pairs([0x8E], range(0xA1, 0xE0))
               + pairs(range(0xA1, 0xFF), range(0xA1, 0xFF), prefix=b"\x8f")),
    "ISO-2022-JP": (iso_2022_jp, pairs(range(0x21, 0x7F), range(0x21, 0x7F), b"\x1b$B", b"\x1b(B")),
}

print("single-byte: bytes 0x80-0xFF decoded unlike the standard's index")
python_total = 0
for name, codec in _SINGLE_BYTE.items():
    index = idx["iso-8859-8" if name == "ISO-8859-8-I" else name.lower()]
    want = ["\ufffd" if cp is None else chr(cp) for cp in index]
    ours = sum(decode(bytes([b]), name) != want[b - 0x80] for b in range(0x80, 0x100))
    python = sum(bytes([b]).decode(codec, "replace") != want[b - 0x80] for b in range(0x80, 0x100))
    python_total += python
    if ours or python:
        print(f"  {name:14} {codec:13} crawler {ours:3}  Python's codec {python:3}")
print(f"  every other single-byte encoding: none; Python's codecs in total: {python_total}")

print("\nmulti-byte: sequences the standard's decoder reads")
print(f"  {'encoding':12} {'codec':15} {'sequences':>9} {'alike':>7} {'different':>9} {'standard only':>13} {'codec only':>10}")
for name, (whatwg, sequences) in MULTI_BYTE.items():
    alike = different = standard_only = codec_only = 0
    for seq in sequences:
        try:
            ours = seq.decode(_MULTI_BYTE[name])
        except UnicodeDecodeError:
            ours = None
        want = whatwg(seq)
        if ours == want:
            alike += want is not None
        elif ours is None:
            standard_only += 1
        elif want is None:
            codec_only += 1
        else:
            different += 1
    total = alike + different + standard_only
    print(f"  {name:12} {_MULTI_BYTE[name]:15} {total:9,} {alike:7,} {different:9,} {standard_only:13,} {codec_only:10,}")
