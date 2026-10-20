import json

from web_crawler.records import error_record, page_record

URL = "https://example.com/blog/"
LINKS = {"https://example.com/": None, "https://example.com/blog/post-1": None}


def test_a_text_record_is_the_url_then_each_link_indented() -> None:
    assert page_record("text", URL, LINKS, status=200, depth=1) == (
        b"https://example.com/blog/\n  https://example.com/\n  https://example.com/blog/post-1\n\n"
    )
    assert page_record("text", URL, [], status=200, depth=0) == b"https://example.com/blog/\n\n"


def test_a_text_record_has_nothing_else() -> None:
    record = page_record(
        "text",
        URL,
        LINKS,
        status=203,
        depth=4,
        redirected_from=("https://example.com/blog",),
        truncated=True,
        canonical="https://example.com/blog/",
    )
    assert record == page_record("text", URL, LINKS, status=200, depth=1)


def test_a_jsonl_record_is_one_line_of_json() -> None:
    # DESIGN.md §5.8's example, field for field.
    record = page_record(
        "jsonl", URL, LINKS, status=200, depth=1, redirected_from=("https://example.com/blog",)
    )
    assert record == (
        b'{"url":"https://example.com/blog/","status":200,"depth":1,"links":'
        b'["https://example.com/","https://example.com/blog/post-1"],'
        b'"redirected_from":["https://example.com/blog"]}\n'
    )


def test_a_jsonl_record_has_the_optional_fields_only_when_they_say_something() -> None:
    plain = json.loads(page_record("jsonl", URL, [], status=200, depth=0))
    assert plain == {"url": URL, "status": 200, "depth": 0, "links": []}
    full = json.loads(
        page_record(
            "jsonl",
            URL,
            LINKS,
            status=203,
            depth=2,
            redirected_from=("https://example.com/a", "https://example.com/b"),
            truncated=True,
            canonical="https://example.com/canonical",
        )
    )
    assert list(full) == [
        "url",
        "status",
        "depth",
        "links",
        "redirected_from",
        "truncated",
        "canonical",
    ]
    assert full["redirected_from"] == ["https://example.com/a", "https://example.com/b"]
    assert (full["truncated"], full["canonical"]) == (True, "https://example.com/canonical")


def test_an_error_record_says_why() -> None:
    assert error_record("https://example.com/missing", 2, "HTTP 404", status=404) == (
        b'{"url":"https://example.com/missing","status":404,"depth":2,"error":"HTTP 404"}\n'
    )


def test_an_error_record_without_a_response_has_no_status() -> None:
    record = json.loads(
        error_record("https://example.com/down", 1, "connect", detail="connect: refused")
    )
    assert record == {
        "url": "https://example.com/down",
        "depth": 1,
        "error": "connect",
        "detail": "connect: refused",
    }


def test_an_error_record_has_the_optional_fields_only_when_they_say_something() -> None:
    record = json.loads(
        error_record(
            "https://example.com/old",
            3,
            "redirect out of scope",
            status=301,
            detail="HTTP 301 to https://elsewhere.org/, outside the crawl's scope",
            redirected_from=("https://example.com/older",),
            location="https://elsewhere.org/",
            content_type="text/plain",
        )
    )
    assert list(record) == [
        "url",
        "status",
        "depth",
        "error",
        "detail",
        "redirected_from",
        "location",
        "content_type",
    ]
    # A detail that says no more than the error isn't repeated.
    assert "detail" not in json.loads(
        error_record("https://example.com/missing", 2, "HTTP 404", status=404, detail="HTTP 404")
    )
