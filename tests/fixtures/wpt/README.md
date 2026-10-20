# URL Standard tests from web-platform-tests

`urltestdata.json` is a subset of web-platform-tests' [`url/resources/urltestdata.json`](https://github.com/web-platform-tests/wpt/blob/c48d58747e1f211527fb695fd60548a997fae617/url/resources/urltestdata.json), at commit `c48d58747e1f211527fb695fd60548a997fae617` (2026-09-19). It keeps, verbatim and in order, the 484 of its 896 test entries that a crawler of http(s) pages relies on:

- every entry whose `base` is an `http:` or `https:` URL, because every link on a page resolves against one;
- every entry with no `base` whose `href` is an http(s) URL, or which is a `failure` whose `input` starts with `http:` or `https:` once the URL parser's leading C0 controls, spaces, tabs and newlines are ignored. These are start URLs, and absolute links, which resolve the same whatever the base.

The upstream file's comment strings are left out.

`tests/unit/test_wpt_urls.py` checks ada-url against every entry, and the crawler's own URL functions against the expected results.

## Licence

web-platform-tests is licensed under the 3-Clause BSD License, © web-platform-tests contributors. [`LICENSE.md`](LICENSE.md) is its licence, copied from the same commit.
