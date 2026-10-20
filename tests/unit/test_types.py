import dataclasses
import pickle

import pytest

from web_crawler.types import CrawlItem, ExitCode, ParseJob, ParseResult, WorkerContext


def test_items_are_immutable() -> None:
    item = CrawlItem(url="https://example.com/", key="https://example.com/", depth=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.depth = 1  # type: ignore[misc]


def test_a_new_item_has_had_no_retries_and_followed_no_redirects() -> None:
    item = CrawlItem(url="https://example.com/", key="https://example.com/", depth=0)
    assert (item.attempt, item.throttled, item.redirected_from) == (0, 0, ())


@pytest.mark.parametrize(
    "value",
    [
        ParseJob(url="https://example.com/", body=b"<a href=/x>"),
        ParseJob("https://example.com/", b"<a href=/\xe9>", "latin1", True, "gzip, br"),
        ParseResult(record=b"r\n", links=[("k", "u")], links_found=1, decoded_bytes=2, error=None),
        ParseResult(b"", [], 0, 0, "bad content-encoding"),
        ParseResult(b"r\n", [], 0, 5, None, truncated=True),
        WorkerContext(("https://example.com/", "http://example.com/"), max_bytes=1 << 20),
        WorkerContext(scope_prefixes=("http://127.0.0.1:8000/",), max_bytes=1, all_urls=True),
    ],
)
def test_parse_contracts_survive_pickling(value: object) -> None:
    # They cross the process boundary to the parser pool by pickle.
    assert pickle.loads(pickle.dumps(value)) == value


def test_exit_codes_match_the_design() -> None:
    assert {code.name: code.value for code in ExitCode} == {
        "OK": 0,
        "INTERNAL_ERROR": 1,
        "USAGE": 2,
        "CANNOT_START": 3,
        "ABORTED": 4,
        "INTERRUPTED": 130,
        "TERMINATED": 143,
    }
