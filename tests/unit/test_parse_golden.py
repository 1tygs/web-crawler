"""Golden fixtures for parse_page: tests/fixtures/parse/NAME.html, a page, and NAME.json, what
it prints and crawls.

Each NAME.json has "about", what the case shows; "printed", the links in the page's record;
and "crawled", the in-scope navigational links it returns to crawl. Optional keys set the
job: "url" (https://example.com/dir/page.html otherwise), "http_charset", "truncated" and
"all_urls".
"""

import json
from pathlib import Path
from typing import Any

import pytest

from web_crawler.config import MIB
from web_crawler.parse import parse_page
from web_crawler.types import ParseJob, WorkerContext

FIXTURES = Path(__file__).parents[1] / "fixtures" / "parse"
SCOPE = ("https://example.com/", "http://example.com/")
CASES = sorted(path.stem for path in FIXTURES.glob("*.json"))


def test_every_page_has_its_expectations() -> None:
    assert sorted(path.stem for path in FIXTURES.glob("*.html")) == CASES


@pytest.mark.parametrize("name", CASES)
def test_golden(name: str) -> None:
    case: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))
    assert case["about"]
    job = ParseJob(
        url=case.get("url", "https://example.com/dir/page.html"),
        body=(FIXTURES / f"{name}.html").read_bytes(),
        http_charset=case.get("http_charset"),
        truncated=case.get("truncated", False),
    )
    result = parse_page(job, WorkerContext(SCOPE, MIB, all_urls=case.get("all_urls", False)))
    assert result.record.decode() == "".join(
        [job.url, "\n", *(f"  {url}\n" for url in case["printed"]), "\n"]
    )
    assert [url for _, url in result.links] == case["crawled"]
    assert result.links_found == len(case["printed"])
