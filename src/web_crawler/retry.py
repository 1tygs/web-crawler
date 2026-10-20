"""Which failures are worth another try, and when (DESIGN.md §5.4).

classify() is the table: a failure is an error, retried with backoff up to --retries times;
throttling, a 429 or 503, which has a budget of its own; or final. next_attempt() says when
an item is requested again, or that it isn't. Both are pure: the engine passes the random
source in, and keeps an item waiting out its backoff in the frontier's retry heap, so
nothing sleeps while holding a permit.
"""

import dataclasses
import enum
import random

from web_crawler.types import CrawlItem, ErrorKind

BASE = 0.5  # seconds: the first retry waits at most this long, and each one after twice that
CAP = 30.0  # seconds: no retry waits longer
THROTTLED_RETRIES = 5  # for 429 and 503, which don't use up --retries
# Past this many retries, BASE * 2 ** retries is above CAP: a bound that keeps the power
# within a float however large --retries is.
_DOUBLINGS = 16


class Class(enum.Enum):
    """What a failure means for its item."""

    ERROR = "error"  # it may not happen again: retried up to --retries times, with backoff
    THROTTLE = "throttle"  # the server asks for fewer requests: retried, with a budget of 5
    FINAL = "final"  # asking again would get the same answer


# No response, or a body cut off: any of these may go away. A response that isn't HTTP, or
# a URL that can't be requested, won't.
_ERRORS = frozenset(
    {
        ErrorKind.DNS,
        ErrorKind.CONNECT,
        ErrorKind.TLS,
        ErrorKind.CONNECT_TIMEOUT,
        ErrorKind.TIMEOUT,
        ErrorKind.RESET,
        ErrorKind.PAYLOAD,
    }
)
_ERROR_STATUSES = frozenset({408, 500, 502, 504})  # timeouts and failures along the way
_THROTTLE_STATUSES = frozenset({429, 503})  # Too Many Requests, Service Unavailable


def classify(answer: int | ErrorKind) -> Class:
    """What a response's status, or the error that kept a request from getting one, means
    for its item: DESIGN.md §5.4's table. Any status it doesn't name is final, 403 among
    them; a run of 403s is the block guard's to judge (limiter.BlockGuard)."""
    if isinstance(answer, ErrorKind):
        return Class.ERROR if answer in _ERRORS else Class.FINAL
    if answer in _THROTTLE_STATUSES:
        return Class.THROTTLE
    return Class.ERROR if answer in _ERROR_STATUSES else Class.FINAL


def backoff(retries: int, rng: random.Random) -> float:
    """Seconds to wait before the next try of an item retried `retries` times so far: full
    jitter, uniform(0, min(CAP, BASE * 2 ** retries)), so that items that failed together
    don't come back together."""
    return rng.uniform(0.0, min(CAP, BASE * 2.0 ** min(retries, _DOUBLINGS)))


def next_attempt(
    item: CrawlItem,
    cls: Class,
    *,
    retries: int,
    rng: random.Random,
    retry_after: float | None = None,
) -> tuple[CrawlItem, float] | None:
    """The item to request again and how many seconds to wait first, or None if it has no
    retries left: then its failure is final.

    An error uses up one of `retries`. Throttling uses up one of THROTTLED_RETRIES instead,
    and waits retry_after seconds, the server's Retry-After, when it gave one.
    """
    match cls:
        case Class.ERROR if item.attempt < retries:
            return dataclasses.replace(item, attempt=item.attempt + 1), backoff(item.attempt, rng)
        case Class.THROTTLE if item.throttled < THROTTLED_RETRIES:
            delay = retry_after if retry_after is not None else backoff(item.throttled, rng)
            return dataclasses.replace(item, throttled=item.throttled + 1), delay
        case _:
            return None
