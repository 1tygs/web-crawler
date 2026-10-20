"""html5lib's character encoding tests (tests/fixtures/html5lib/README.md)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from web_crawler.content import Source
from web_crawler.encodings import WINDOWS_1252, get_encoding
from web_crawler.parse import parse_html

HTML5LIB = Path(__file__).parents[1] / "fixtures" / "html5lib"


def cases(path: Path) -> Iterator[tuple[bytes, str]]:
    """Each test's #data, without its last newline, and its #encoding."""
    lines = path.read_bytes().split(b"\n")
    for start, line in enumerate(lines):
        if line == b"#data":
            end = lines.index(b"#encoding", start)
            yield b"\n".join(lines[start + 1 : end]), lines[end + 1].decode("ascii")


CASES = [
    case
    for name in ("tests1.dat", "tests2.dat", "test-yahoo-jp.dat")
    for case in cases(HTML5LIB / name)
]


def test_every_case_is_read() -> None:
    assert len(CASES) == 82


@pytest.mark.parametrize(("data", "expected"), CASES, ids=range(len(CASES)))
def test_decodes_with_the_encoding_a_browser_picks(data: bytes, expected: str) -> None:
    _, charset = parse_html(data, None)
    # The tests assume a browser whose default is windows-1252, where the crawler guesses.
    picked = WINDOWS_1252 if charset.source is Source.GUESS else charset.encoding
    assert picked == get_encoding(expected)
