"""URL rules: the start URL, the crawl's scope, fragments and dedupe keys (DESIGN.md §2.4,
§5.5, §5.6).

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

# RFC 3986 §2.3: the characters that mean the same whether or not they're percent-encoded.
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX_DIGITS = "0123456789ABCDEFabcdef"
# Each escape, mapped to its normal form: the character, if it's unreserved, and otherwise
# the escape with uppercase hex digits.
_ESCAPES = {
    f"%{high}{low}": (
        char if (char := chr(int(high + low, 16))) in _UNRESERVED else f"%{high}{low}".upper()
    )
    for high in _HEX_DIGITS
    for low in _HEX_DIGITS
}
# A hex digit, written as itself or as its escape, mapped to its escape.
_HEX_DIGIT_ESCAPES = {
    form: f"%{ord(digit):02X}" for digit in _HEX_DIGITS for form in (digit, f"%{ord(digit):02X}")
}
_HEX_DIGIT = r"(?:[0-9A-Fa-f]|%(?:3[0-9]|4[1-6]|6[1-6]))"  # as itself or as its escape
# An escape; or a "%" that starts none, with the first of two hex digits that decoding would
# put after it. WHATWG leaves such a "%" alone, and decoding them would make it start one:
# /%%34%31 must not become /%41, which already means /A.
_TO_NORMALIZE = re.compile(rf"%[0-9A-Fa-f]{{2}}|%{_HEX_DIGIT}(?={_HEX_DIGIT})")


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

    host: str  # as WHATWG serializes it: an IPv6 literal keeps its brackets
    prefixes: tuple[str, ...]  # every in-scope display URL starts with one of these
    origins: tuple[str, ...]  # the origins whose robots.txt applies

    @classmethod
    def for_url(cls, url: str) -> Self:
        parsed = ada_url.URL(url)
        if parsed.protocol not in ("http:", "https:"):
            raise ValueError(f"not an http or https URL: {url!r}")
        host = parsed.hostname
        if parsed.port:
            origins: tuple[str, ...] = (f"{parsed.protocol}//{host}:{parsed.port}",)
        else:
            origins = (f"https://{host}", f"http://{host}")
        return cls(host, tuple(f"{origin}/" for origin in origins), origins)

    def contains(self, url: str) -> bool:
        """Whether a display URL is in scope. Look-alikes such as example.com.evil.net,
        example.com:8443 and example.com@evil.net fail without being parsed."""
        return url.startswith(self.prefixes)

    def www_switch(self, url: str) -> Scope | None:
        """The scope a start URL moves to when it redirects to url (DESIGN.md §2.4): url's
        host, if it only adds or removes "www." from this scope's, on the same ports and
        schemes. None for any other URL."""
        try:
            hostname = ada_url.URL(url).hostname
            switched = Scope.for_url(url)
        except ValueError:
            return None
        same_ports = switched.origins == tuple(
            origin.replace(f"//{self.host}", f"//{hostname}", 1) for origin in self.origins
        )
        return switched if same_ports and is_www_variant(self.host, hostname) else None


def is_www_variant(a: str, b: str) -> bool:
    """Whether two hosts differ only by a "www." label at the start of one of them."""
    return a == f"www.{b}" or b == f"www.{a}"


def strip_fragment(url: str) -> str:
    """url without its fragment, or url itself if it has none.

    In a WHATWG serialization the first "#" starts the fragment: the parser ends every
    other component at a "#", so no other component can contain one.
    """
    hash_at = url.find("#")
    return url if hash_at == -1 else url[:hash_at]


def dedupe_key(url: str) -> str:
    """The key that identifies a display URL's resource (DESIGN.md §5.6).

    It applies the RFC 3986 §6.2.2 equivalences that WHATWG serialization leaves alone:
    escapes of unreserved characters are decoded (%7E is ~) and other escapes get uppercase
    hex digits (%2f is %2F). URLs with the same key differ in nothing else, and a URL with no
    "%", which is most of them, is its own key: the same object comes back.
    """
    if "%" not in url:
        return url
    return _TO_NORMALIZE.sub(_normalize, url)


def _normalize(match: re.Match[str]) -> str:
    found = match[0]
    # Otherwise found is a "%" that starts no escape, then a hex digit, which stays encoded.
    return _ESCAPES.get(found) or "%" + _HEX_DIGIT_ESCAPES[found[1:]]
