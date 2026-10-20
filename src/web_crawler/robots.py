"""robots.txt by RFC 9309 (DESIGN.md §5.7): what each origin's file says, and what a crawl
may fetch.

This is the pure part. fetcher.RobotsFetcher fetches the files, and turns each answer into
a RobotsTxt here: a file that was found is parsed with Protego; one that's unavailable, such
as a 404, allows everything; one that's unreachable, through server errors or no answer at
all, disallows everything.
"""

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus

from protego import Protego

MAX_REDIRECTS = 5  # RFC 9309 asks crawlers to follow at least five
MAX_BYTES = 500 * 1024  # the parsing limit, which RFC 9309 says must be at least 500 KiB
TRIES = 3  # for an unreachable file, before everything is disallowed

_SERVER_ERRORS = range(500, 600)

_PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]+")


class Availability(enum.Enum):
    FOUND = "found"  # its rules apply
    UNAVAILABLE = "unavailable"  # 4xx and the like: everything is allowed (RFC 9309 §2.3.1.3)
    UNREACHABLE = "unreachable"  # 429, 5xx or no answer: everything is disallowed (§2.3.1.4)


@dataclass(frozen=True, slots=True)
class RobotsTxt:
    """One origin's robots.txt, as fetched."""

    url: str  # where it was asked for, at the origin it governs
    availability: Availability
    detail: str  # what the answer was, for messages: "HTTP 404", "HTTP 503, 3 tries", ...
    rules: Protego | None = None  # for a file that was found
    no_answer: str | None = None  # the transport error kind, if the host never answered

    @classmethod
    def found(cls, url: str, body: bytes, *, truncated: bool) -> RobotsTxt:
        """A file read from body, its first MAX_BYTES; the rest, if any, is ignored."""
        if truncated:  # a rule cut off at the limit could say something else
            body = body[: body.rfind(b"\n") + 1]
        # RFC 9309 files are UTF-8. A byte order mark would hide the first line from
        # Protego, so utf-8-sig drops it.
        text = body.decode("utf-8-sig", "replace")
        detail = f"{len(body):,} bytes" + (", truncated" if truncated else "")
        return cls(url, Availability.FOUND, detail, Protego.parse(text))

    def allowed(self, url: str, product: str) -> bool:
        if self.rules is not None:
            return self.rules.can_fetch(url, product)
        return self.availability is Availability.UNAVAILABLE

    def crawl_delay(self, product: str) -> float | None:
        return None if self.rules is None else self.rules.crawl_delay(product)


def unreachable(status: int) -> bool:
    """Whether a robots.txt answer with status means the file is unreachable, for now: a
    server error (RFC 9309 §2.3.1.4), or 429, which RFC 9309 doesn't name, but reading "too
    many requests" as permission would be the wrong way round."""
    return status == HTTPStatus.TOO_MANY_REQUESTS or status in _SERVER_ERRORS


def without_file(url: str, status: int) -> RobotsTxt:
    """What a non-2xx answer that isn't a redirect means for the robots.txt at url."""
    availability = Availability.UNREACHABLE if unreachable(status) else Availability.UNAVAILABLE
    return RobotsTxt(url, availability, f"HTTP {status}")


def product_token(user_agent: str) -> str:
    """The product token a robots.txt group names a crawler by: web-crawler for
    "web-crawler/1.0 (+https://...)". RFC 9309 §2.2.1 allows letters, "_" and "-"; a User-Agent
    that starts with anything else has none, and only the "*" group applies to it."""
    match = _PRODUCT_TOKEN.match(user_agent)
    return match[0] if match else ""


class RobotsPolicy:
    """What a crawl may fetch: the robots.txt of each origin in scope, matched on the
    crawler's product token.

    Protego matches a group whose name starts a token in the product token, so a group for
    "web" applies to web-crawler, where RFC 9309 would match the whole token (DESIGN.md
    A.6). With no files, as under --ignore-robots, everything is allowed.
    """

    def __init__(self, product: str, files: Mapping[str, RobotsTxt]) -> None:
        self.product = product
        self.files = dict(files)  # by origin, as in Scope.origins
        self._by_prefix = tuple((f"{origin}/", file) for origin, file in self.files.items())

    def file_for(self, url: str) -> RobotsTxt | None:
        """The robots.txt that governs url, a display URL, if one was fetched for its
        origin."""
        for prefix, file in self._by_prefix:
            if url.startswith(prefix):
                return file
        return None

    def allowed(self, url: str) -> bool:
        file = self.file_for(url)
        return file is None or file.allowed(url, self.product)

    @property
    def crawl_delay(self) -> float | None:
        """The longest Crawl-delay among the origins, for the one host they share."""
        return max(
            (delay for file in self.files.values() if (delay := file.crawl_delay(self.product))),
            default=None,
        )
