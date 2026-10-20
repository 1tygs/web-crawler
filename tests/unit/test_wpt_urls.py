"""The URL Standard's own tests, from web-platform-tests (tests/fixtures/wpt/README.md).

ada-url runs the whole suite upstream; this pins the part the crawler relies on to the
version installed here, and checks the crawler's URL functions against the same results.
"""

import json
from pathlib import Path
from typing import Any

import ada_url
import pytest

from web_crawler.urls import Scope, StartURLError, dedupe_key, normalize_start, strip_fragment

CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "wpt" / "urltestdata.json").read_text("utf-8")
)
PARSED = [case for case in CASES if not case.get("failure")]
HTTP = [case for case in PARSED if case["protocol"] in ("http:", "https:")]


def parse(case: dict[str, Any]) -> str:
    if case["base"] is None:
        return ada_url.URL(case["input"]).href
    return ada_url.join_url(case["base"], case["input"])


def test_the_subset_covers_both_kinds_of_case() -> None:
    assert len(CASES) == 484
    assert len(PARSED) == 283
    assert {case["base"] is None for case in CASES} == {True, False}


@pytest.mark.parametrize("case", CASES, ids=range(len(CASES)))
def test_ada_url_parses_as_the_standard_says(case: dict[str, Any]) -> None:
    if case.get("failure"):
        with pytest.raises(ValueError):  # noqa: PT011 (ada-url raises a bare ValueError)
            parse(case)
    else:
        assert parse(case) == case["href"]


@pytest.mark.parametrize("case", PARSED, ids=range(len(PARSED)))
def test_strip_fragment_removes_the_standards_fragment(case: dict[str, Any]) -> None:
    # hash is "" for an empty fragment too, which href still writes as "#".
    assert strip_fragment(case["href"]) == case["href"].removesuffix(case["hash"] or "#")


@pytest.mark.parametrize("case", HTTP, ids=range(len(HTTP)))
def test_a_url_is_in_its_own_scope_unless_it_has_credentials(case: dict[str, Any]) -> None:
    url = strip_fragment(case["href"])
    has_credentials = bool(case["username"] or case["password"])
    assert Scope.for_url(url).contains(url) is not has_credentials


@pytest.mark.parametrize("case", HTTP, ids=range(len(HTTP)))
def test_dedupe_key_is_idempotent_on_real_urls(case: dict[str, Any]) -> None:
    key = dedupe_key(strip_fragment(case["href"]))
    assert dedupe_key(key) == key


START_CASES = [case for case in CASES if case["base"] is None]


@pytest.mark.parametrize("case", START_CASES, ids=range(len(START_CASES)))
def test_normalize_start_agrees_with_the_standard(case: dict[str, Any]) -> None:
    # Each input has an explicit http(s) scheme, so it parses as written.
    if case.get("failure") or case["username"] or case["password"]:
        with pytest.raises(StartURLError):
            normalize_start(case["input"])
    else:
        assert normalize_start(case["input"]) == (strip_fragment(case["href"]), False)
