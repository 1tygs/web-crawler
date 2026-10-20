import dataclasses
import pickle

import pytest

from web_crawler.types import CrawlItem, ExitCode, ParseJob, ParseResult, WorkerContext


def test_items_are_immutable() -> None:
    item = CrawlItem(url="https://example.com/", key="https://example.com/", depth=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.depth = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    [
        ParseJob(url="https://example.com/", body=b"<a href=/x>"),
        ParseResult(record=b"r\n", links=[("k", "u")], links_found=1),
        WorkerContext(scope_prefixes=("https://example.com/", "http://example.com/")),
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
        "INTERRUPTED": 130,
    }
