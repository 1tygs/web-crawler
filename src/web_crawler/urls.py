"""URL rules: the start URL, the crawl's scope, fragments, dedupe keys, trap rules and the
extension filter (DESIGN.md §2.4, §5.1, §5.5, §5.6).

Every URL past normalize_start is a WHATWG serialization from ada-url, which is canonical:
scheme and host are lowercase, the host is punycode, a default port is omitted, and a path
starts with "/". That is what lets the scope check be a prefix test on the string, and the
rules read a path without parsing the URL.
"""

import re
from collections import Counter
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


# The trap rules' thresholds (DESIGN.md §5.1), which Config can change.
MAX_URL_LENGTH = 2048  # characters: the sitemap protocol's limit
MAX_SEGMENT_REPEATS = 3  # times one segment may occur in a path
MAX_QUERY_VARIANTS = 1000  # distinct query strings for one path

# What each rule is called in the summary's "not crawled" counts.
LONG_URL = "URL too long"
REPEATED_SEGMENT = "path segment repeated"
QUERY_VARIANTS = "too many query variants"
SKIPPED_EXTENSION = "file extension"

# Extensions of files that are almost never HTML, by DESIGN.md §5.1's categories: fetching
# one would only be to throw it away unparsed. A link to one is still printed.
_SKIPPED = {
    "images": "apng avif bmp cur gif heic heif ico jfif jpe jpeg jpg jxl pjp pjpeg png psd svg "
    "svgz tif tiff webp",
    "audio and video": "3g2 3gp aac aif aiff avi flac flv m4a m4v mid midi mkv mov mp3 mp4 mpeg "
    "mpg oga ogg ogv opus qt wav weba webm wma wmv",
    "archives and binaries": "7z apk appimage bin bz2 cab deb dmg ear exe gz img iso jar lz "
    "lzma msi pkg rar rpm swf tar tbz2 tgz txz war wasm xz zip zst",
    "fonts": "eot otf ttf woff woff2",
    "PDF and office documents": "doc docm docx dot dotx epub odg odp ods odt pdf pps ppsx ppt "
    "pptx rtf xls xlsm xlsx xlt",
    "style sheets and scripts": "css js mjs",
}
SKIPPED_EXTENSIONS = frozenset(name for names in _SKIPPED.values() for name in names.split())


def trap_rule(
    url: str, max_length: int = MAX_URL_LENGTH, max_repeats: int = MAX_SEGMENT_REPEATS
) -> str | None:
    """The trap rule that url, a display URL, breaks, if any (DESIGN.md §5.1): it's longer
    than max_length characters, or a segment occurs more than max_repeats times in its
    path, as in /a/b/a/b/a/b/a/b from relative links that a soft-404 site answers whatever
    they lead to. Rules that a crawl can't check one URL at a time, such as the query
    strings a path has, are Admission's."""
    if len(url) > max_length:
        return LONG_URL
    start, end = _path_span(url)
    # Cheap tests first, as most paths have few segments, and those of most others differ.
    if url.count("/", start, end) <= max_repeats:
        return None
    segments = url[start + 1 : end].split("/")
    if len(set(segments)) > len(segments) - max_repeats:
        return None
    return REPEATED_SEGMENT if max(Counter(segments).values()) > max_repeats else None


def has_skipped_extension(url: str) -> bool:
    """Whether url, a display URL, names a file that's almost never HTML: the last segment
    of its path ends in one of SKIPPED_EXTENSIONS, in any case (DESIGN.md §5.1)."""
    end = url.find("?")
    if end == -1:
        end = len(url)
    # The last "/" is the path's, as the host can't contain one.
    dot = url.rfind(".", url.rfind("/", 0, end), end)
    return dot != -1 and url[dot + 1 : end].lower() in SKIPPED_EXTENSIONS


def without_query(url: str) -> str:
    """url up to its query, if it has one: the path's URL, of which a query is a variant."""
    question = url.find("?")
    return url if question == -1 else url[:question]


def _path_span(url: str) -> tuple[int, int]:
    """Where a display URL's path starts and ends: from the "/" after the host, which can't
    contain one, up to the query, if any."""
    start = url.find("/", url.find("//") + 2)
    end = url.find("?", start)
    return start, len(url) if end == -1 else end


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
