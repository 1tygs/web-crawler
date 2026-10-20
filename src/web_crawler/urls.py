"""URL rules: the start URL, the crawl's scope and fragments (DESIGN.md §2.4, §5.5).

Every URL past normalize_start is a WHATWG serialization from ada-url, which is canonical:
scheme and host are lowercase, the host is punycode, a default port is omitted, and a path
starts with "/". That is what lets the scope check be a prefix test on the string.
"""

import re
from dataclasses import dataclass
from typing import Self

import ada_url

# What the WHATWG URL parser strips from both ends, and removes everywhere.
_C0_CONTROL_OR_SPACE = "".join(map(chr, range(0x21)))
_TAB_OR_NEWLINE = dict.fromkeys(map(ord, "\t\n\r"))

_HTTP_SCHEME = re.compile(r"https?:", re.IGNORECASE)
_SCHEME_AND_SLASHES = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


class StartURLError(ValueError):
    """The start URL can't be crawled. The message says why."""


def normalize_start(raw: str) -> tuple[str, bool]:
    """Return the start URL as a display URL, and whether its scheme was implicit.

    Input with no scheme gets https://, so that localhost:8000 isn't read as the scheme
    "localhost:" and the path "8000". Input that starts with http: or https: keeps its
    scheme even without the slashes, as browsers read https:/example.com, rather than
    becoming https://https:/example.com, a URL for the host "https".
    """
    text = raw.strip(_C0_CONTROL_OR_SPACE).translate(_TAB_OR_NEWLINE)
    implicit = not (_HTTP_SCHEME.match(text) or _SCHEME_AND_SLASHES.match(text))
    try:
        url = ada_url.URL(f"https://{text}" if implicit else text)
    except ValueError:
        raise StartURLError(f"{raw!r} isn't a valid URL") from None
    if url.protocol not in ("http:", "https:"):
        raise StartURLError(f"{raw!r} isn't an http or https URL")
    if url.username or url.password:
        # Authenticated areas are out of scope (DESIGN.md §2.5), and every in-scope link
        # would fail a scope check that includes the credentials.
        raise StartURLError(f"{raw!r} contains credentials, which the crawler doesn't use")
    return strip_fragment(url.href), implicit


@dataclass(frozen=True, slots=True)
class Scope:
    """The one host a crawl fetches from (DESIGN.md §2.4).

    On the default ports, http and https on that host are both in scope. With any other
    port, only the start URL's own scheme and port are: another port is another service.
    """

    prefixes: tuple[str, ...]  # every in-scope display URL starts with one of these
    origins: tuple[str, ...]  # the origins whose robots.txt applies

    @classmethod
    def for_url(cls, url: str) -> Self:
        parsed = ada_url.URL(url)
        if parsed.protocol not in ("http:", "https:"):
            raise ValueError(f"not an http or https URL: {url!r}")
        host = parsed.hostname  # an IPv6 literal keeps its brackets
        if parsed.port:
            origins: tuple[str, ...] = (f"{parsed.protocol}//{host}:{parsed.port}",)
        else:
            origins = (f"https://{host}", f"http://{host}")
        return cls(prefixes=tuple(f"{origin}/" for origin in origins), origins=origins)

    def contains(self, url: str) -> bool:
        """Whether a display URL is in scope. Look-alikes such as example.com.evil.net,
        example.com:8443 and example.com@evil.net fail without being parsed."""
        return url.startswith(self.prefixes)


def strip_fragment(url: str) -> str:
    """url without its fragment, or url itself if it has none.

    In a WHATWG serialization the first "#" starts the fragment: the parser ends every
    other component at a "#", so no other component can contain one.
    """
    hash_at = url.find("#")
    return url if hash_at == -1 else url[:hash_at]
