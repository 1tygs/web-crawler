# Character encoding tests from html5lib-tests

`tests1.dat`, `tests2.dat` and `test-yahoo-jp.dat` are copied verbatim from [html5lib-tests](https://github.com/html5lib/html5lib-tests)' [`encoding/`](https://github.com/html5lib/html5lib-tests/tree/c777c408b61078ea2eb4acefc2535f54dbc8b28a/encoding) folder at commit `c777c408b61078ea2eb4acefc2535f54dbc8b28a` (2026-10-01). The folder last changed in commit `0e9ed8e` (2020-08-21).

Each test is a `#data` section, the start of a page, and the `#encoding` a browser decodes it with when nothing else declares one and its default is windows-1252. The tests cover the byte order mark, the HTML Standard's prescan of the first 1,024 bytes, and a `<meta>` the parser reaches later. `tests/unit/test_html5lib_encoding.py` runs all 82 through `parse.parse_html`, reading its guess for an undeclared page as that default.

## Licence

html5lib-tests is licensed under the MIT License, © 2006-2013 James Graham, Geoffrey Sneddon, and other contributors. [`LICENSE`](LICENSE) is its licence, copied from the same commit.
