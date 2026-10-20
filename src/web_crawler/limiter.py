"""How many requests a crawl keeps in flight, and how fast it starts them (DESIGN.md §5.3).

ConcurrencyLaw is ADR-001's law: errors cut the limit, latency only stops it growing, and a
probe confirms that a slowdown is the crawl's own doing. PauseGate holds request starts back
for as long as a Retry-After asks, and spaces them under --max-rps and robots.txt's
Crawl-delay. All of it is pure: the engine passes the time in, and bench/limiter_sim.py runs
the same law on simulated servers.
"""

import email.utils
import enum
import math
import statistics
from dataclasses import dataclass
from datetime import UTC

START = 8  # the limit a crawl starts with, in slow start
INFLATED = 1.5  # a round's median this many times the base: the server is queueing
HELD_ROUNDS = 10  # rounds held in a row before a probe checks whose queueing it is
PROBE_ROUNDS = 2
OURS = 0.8  # a probe whose median falls to this share of the held one: the queueing was ours
MAX_PAUSE = 300.0  # seconds: the longest a Retry-After holds requests back


class Kind(enum.Enum):
    BASE = "base"  # the first round measured the server at low load
    GROW = "grow"
    HOLD = "hold"  # the server is queueing, or its pages got slower
    SLOW_START_EXIT = "slow start exit"  # the first rounds' doubling went too far
    PROBE = "probe"  # halved, to see whether the queueing is the crawl's own
    PROBE_OURS = "probe: ours"  # it was: resume at 3/4 of the old limit
    PROBE_NOT_OURS = "probe: not ours"  # slower pages or other users: rebase, resume
    CUT = "cut"  # the server signalled distress


@dataclass(frozen=True, slots=True)
class Decision:
    """A change of the law's state, for the -v log and for tests."""

    kind: Kind
    previous: int  # the limit before
    limit: int  # the limit after
    median: float | None = None  # the round's median time to first byte, in seconds
    base: float | None = None  # the baseline it was compared with

    def __str__(self) -> str:
        text = f"{self.kind.value}: {self.previous} -> {self.limit}"
        if self.median is not None:
            text += f", median {self.median * 1000:.1f} ms"
        if self.base is not None:
            text += f", base {self.base * 1000:.1f} ms"
        return text


class ConcurrencyLaw:
    """ADR-001's limit on requests in flight (DESIGN.md §5.3).

    The limit starts at 8, or the ceiling if that's lower, and doubles each round until a
    round's median time to first byte reaches 1.5 times the first round's, the server at low
    load; that ends slow start, halving the limit. From then on a round below 1.5 times the
    base grows the limit by one, and one above holds it. After 10 held rounds in a row, a
    probe halves the limit for two rounds, counting only requests issued during the probe:
    if their median is 20% lower, the queueing was the crawl's own and the limit resumes at
    3/4 of what it was; otherwise the pages themselves got slower, so their median is the
    new base and the limit resumes.

    A round is the responses to about `limit` requests. Each sample is a response's time to
    first byte; congestion is a 429 or 503, a timeout, a reset or a refused connection.
    Congestion halves the limit once per event: only for a request issued after the last
    cut, which is "once per round trip" made exact. A cut also ends slow start, cancels a
    probe and discards the partial round.
    """

    def __init__(self, ceiling: int) -> None:
        if ceiling < 1:
            raise ValueError(f"ceiling must be at least 1, not {ceiling}")
        self.ceiling = ceiling
        self.limit = min(START, ceiling)
        self._slow_start = True
        self._base: float | None = None
        self._round: list[float] = []
        self._held = 0  # rounds held in a row
        self._probing = 0  # probe rounds left
        self._probe_started = -math.inf
        self._held_median = 0.0  # the median that started the probe
        self._resume = 0  # the limit when the probe started
        self._last_cut = -math.inf

    def on_sample(self, ttfb: float, issued_at: float, now: float) -> Decision | None:
        """A response's time to first byte, for a request issued at issued_at. Returns the
        decision a completed round makes, if any."""
        if self._probing and issued_at < self._probe_started:
            return None  # sent before the probe: says nothing about the lower load
        self._round.append(ttfb)
        if len(self._round) < self.limit:
            return None
        median = statistics.median(self._round)
        self._round = []
        if self._base is None:
            self._base = median
            return Decision(Kind.BASE, self.limit, self.limit, median, median)
        if self._probing:
            return self._probe_round(median, self._base)
        if self._slow_start:
            return self._slow_start_round(median, self._base)
        return self._round_done(median, self._base, now)

    def _probe_round(self, median: float, base: float) -> Decision | None:
        self._probing -= 1
        if self._probing:
            return None
        previous = self.limit
        if median <= OURS * self._held_median:
            self.limit = max(1, self._resume * 3 // 4)
            return Decision(Kind.PROBE_OURS, previous, self.limit, median, base)
        self._base, self.limit = median, self._resume
        return Decision(Kind.PROBE_NOT_OURS, previous, self.limit, median, median)

    def _slow_start_round(self, median: float, base: float) -> Decision:
        previous = self.limit
        if median >= INFLATED * base:
            self._slow_start = False
            self.limit = max(1, previous // 2)
            return Decision(Kind.SLOW_START_EXIT, previous, self.limit, median, base)
        self.limit = min(self.ceiling, previous * 2)
        return Decision(Kind.GROW, previous, self.limit, median, base)

    def _round_done(self, median: float, base: float, now: float) -> Decision:
        """A round after slow start, outside a probe."""
        previous = self.limit
        if median < INFLATED * base:
            self._held = 0
            self.limit = min(self.ceiling, previous + 1)
            return Decision(Kind.GROW, previous, self.limit, median, base)
        self._held += 1
        if self._held < HELD_ROUNDS:
            return Decision(Kind.HOLD, previous, self.limit, median, base)
        self._held = 0
        self._probing = PROBE_ROUNDS
        self._probe_started = now
        self._held_median, self._resume = median, previous
        self.limit = max(1, previous // 2)
        return Decision(Kind.PROBE, previous, self.limit, median, base)

    def on_congestion(self, issued_at: float, now: float) -> Decision | None:
        """A sign of distress for a request issued at issued_at. Returns the cut, or None if
        the request was issued before the last one: it's part of a congestion event that the
        limit has already answered."""
        if issued_at <= self._last_cut:
            return None
        self._last_cut = now
        previous = self.limit
        self.limit = max(1, self.limit // 2)
        self._slow_start = False
        self._probing = 0
        self._held = 0
        self._round = []
        return Decision(Kind.CUT, previous, self.limit)


class PauseGate:
    """When the next request may start (DESIGN.md §5.3).

    A pause, from a 429 or 503's Retry-After, holds every start back until it ends; a later
    pause extends it, an earlier one doesn't shorten it. Under a rate ceiling, each start
    makes the next wait 1/rate seconds, so requests are spaced evenly rather than sent in
    bursts, and a quiet spell earns no catching up. With neither, requests start whenever
    the limit allows.
    """

    def __init__(self, rate: float | None = None) -> None:
        self.rate = rate  # requests per second, or None
        self._next = -math.inf  # the rate ceiling's next start
        self._paused_until = -math.inf

    def delay(self, now: float) -> float:
        """Seconds until a request may start: 0 if one may start now."""
        next_start = max(self._paused_until, self._next if self.rate else -math.inf)
        return max(0.0, next_start - now)

    def take(self, now: float) -> None:
        """A request starts now."""
        if self.rate:
            self._next = now + 1 / self.rate

    def pause(self, until: float) -> None:
        """Start no request before until, the clock's reading when a Retry-After ends."""
        self._paused_until = max(self._paused_until, until)


def parse_retry_after(value: str | None, now_wall: float) -> float | None:
    """The seconds a Retry-After header asks a client to wait (RFC 9110 §10.2.3), at most
    MAX_PAUSE: its number of seconds, or the time from now_wall, the wall clock's reading in
    seconds since the epoch, until its date. None if there's no header, or it's neither.

    A date may be in any of HTTP's three formats. The asctime one has no zone, and is GMT
    as HTTP has it: email.utils reads it as local time.
    """
    if value is None:
        return None
    value = value.strip(" \t")
    if value.isascii() and value.isdigit():  # isdigit() alone accepts other scripts' digits
        return float(min(int(value), MAX_PAUSE))
    try:
        date = email.utils.parsedate_to_datetime(value)
    except ValueError:
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    return min(max(date.timestamp() - now_wall, 0.0), MAX_PAUSE)


def rate_ceiling(max_rps: float | None, crawl_delay: float | None) -> float | None:
    """Requests per second at most: --max-rps, or robots.txt's Crawl-delay of d seconds
    between requests, 1/d per second, whichever is lower. None if neither applies."""
    rates = [rate for rate in (max_rps, 1 / crawl_delay if crawl_delay else None) if rate]
    return min(rates, default=None)
