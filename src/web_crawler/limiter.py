"""How many requests a crawl keeps in flight, and how fast it starts them (DESIGN.md §5.3).

ConcurrencyLaw is ADR-001's law: errors cut the limit, latency only stops it growing, and a
probe confirms that a slowdown is the crawl's own doing. PauseGate holds request starts back
for as long as a Retry-After asks, and spaces them under --max-rps and robots.txt's
Crawl-delay. CircuitBreaker stops the crawl when the host can't be reached, and BlockGuard
when it seems to block the crawl, until a probe finds it well again. All of it is pure: the
engine passes the time in, and bench/limiter_sim.py runs the same law on simulated servers.
"""

import email.utils
import enum
import math
import statistics
from dataclasses import dataclass
from datetime import UTC

from web_crawler.types import CrawlItem, ErrorKind

START = 8  # the limit a crawl starts with, in slow start
INFLATED = 1.5  # a round's median this many times the base: the server is queueing
HELD_ROUNDS = 10  # rounds held in a row before a probe checks whose queueing it is
PROBE_ROUNDS = 2
OURS = 0.8  # a probe whose median falls to this share of the held one: the queueing was ours
MAX_PAUSE = 300.0  # seconds: the longest a Retry-After holds requests back
OPENS_AFTER = 20  # connection-level failures in a row that open the circuit breaker
BLOCKED_AFTER = 20  # 403 Forbidden answers in a row that look like a block
LONGEST_PROBE = 60.0  # seconds between probes of a host the crawl paused for, at most


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


class Probes:
    """When to check on a host the crawl has paused for, and when to give up on it
    (DESIGN.md §5.3, §5.4). The first probe is due `first` seconds after the pause began.
    After each probe that fails, the wait for the next doubles, up to LONGEST_PROBE. The
    crawl gives up give_up_after seconds after the pause began."""

    def __init__(self, first: float, give_up_after: float) -> None:
        self.first = first
        self.give_up_after = give_up_after
        self.began = -math.inf  # when the pause began
        self.next = -math.inf  # when the next probe is due
        self._wait = first

    def start(self, now: float) -> None:
        """A pause begins now."""
        self.began = now
        self._wait = self.first
        self.next = now + self._wait

    def failed(self, now: float) -> None:
        """A probe, at now, found the host no better."""
        self._wait = min(2 * self._wait, LONGEST_PROBE)
        self.next = now + self._wait

    @property
    def deadline(self) -> float:
        """When to give up."""
        return self.began + self.give_up_after


# Failures to set up a connection: they say the host can't be reached, where a reset or a
# timeout, once connected, says it can.
UNREACHABLE = frozenset(
    {ErrorKind.DNS, ErrorKind.CONNECT, ErrorKind.TLS, ErrorKind.CONNECT_TIMEOUT}
)


class CircuitBreaker:
    """Stops a crawl spending its retries on a host it can't reach (DESIGN.md §5.3).

    OPENS_AFTER connection-level failures in a row, DNS, connect, TLS or connect timeout,
    open it: the crawl starts no requests, and probes of the start page check whether the
    host can be reached again, backing off from `first_probe` seconds to LONGEST_PROBE. A
    response closes it, and starts the count again. One that stays open for give_up_after
    seconds gives up, and the crawl aborts.

    Only a response to a request sent after the failures counts, as for the concurrency
    law's cuts: one already on its way when the host went says nothing about it now. A
    reset or a timeout came after the connection was made, so it neither counts nor
    breaks a run.
    """

    def __init__(self, *, first_probe: float = 1.0, give_up_after: float = 300.0) -> None:
        self.open = False
        self.probes = Probes(first_probe, give_up_after)
        self.last: ErrorKind | None = None  # the latest failure that counted
        self._failures = 0  # in a row
        self._since = -math.inf  # when the request of the latest that counted was sent

    def failed(self, kind: ErrorKind, issued_at: float, now: float) -> bool:
        """A request sent at issued_at got no response. Returns whether that opened the
        breaker."""
        if kind not in UNREACHABLE or self.open:
            return False
        self.last = kind
        self._failures += 1
        self._since = max(self._since, issued_at)
        if self._failures < OPENS_AFTER:
            return False
        self.open = True
        self.probes.start(now)
        return True

    def answered(self, issued_at: float) -> bool:
        """A request sent at issued_at got a response. Returns whether that closed the
        breaker."""
        if issued_at <= self._since:
            return False
        self._failures = 0
        was_open, self.open = self.open, False
        return was_open


class Verdict(enum.Enum):
    """What the block guard makes of a 403."""

    HELD = "held"  # its fate waits on the rest of the run
    TRIPPED = "tripped"  # held, and the run is long enough to look like a block
    FINAL = "final"  # the page is forbidden


class BlockGuard:
    """Tells a block, a firewall refusing the crawl, from forbidden pages (DESIGN.md §5.4).

    A 403 is held while the run of 403s it belongs to is under BLOCKED_AFTER: its page
    stays outstanding, with no outcome yet, so that a page tried again later has only one,
    and --max-depth stays exact. A run is settled in one of three ways:
    - a response other than 403, to a request sent after the run's earliest: the host serves
      the crawl, so the held 403s are final;
    - the run reaching BLOCKED_AFTER trips the guard: the crawl pauses, and probes of the
      start page back off as the circuit breaker's do. The first answer other than a 403
      ends the block, and the held pages are tried again, once: a page tried again that
      gets another 403 is forbidden. If none comes in give_up_after seconds, the crawl
      aborts;
    - nothing else can move the crawl on, so nothing else can settle it: the held 403s are
      final.

    A response to a request sent before the run's earliest 403'd one says nothing about the
    block: the host may have answered it before the block began. The start URL is a page,
    so every 403 a crawl's dispatcher sees comes after a page, as the rule asks.
    """

    def __init__(self, *, first_probe: float = 1.0, give_up_after: float = 300.0) -> None:
        self.tripped = False
        self.probes = Probes(first_probe, give_up_after)
        self._held: list[CrawlItem] = []
        self._since = math.inf  # when the request of the run's earliest 403 was sent
        self._requeued: set[str] = set()  # keys of the pages tried again after a block

    @property
    def held(self) -> int:
        return len(self._held)

    def forbidden(self, item: CrawlItem, issued_at: float, now: float) -> Verdict:
        """item got a 403 for a request sent at issued_at."""
        if item.key in self._requeued:
            return Verdict.FINAL
        self._held.append(item)
        self._since = min(self._since, issued_at)
        if self.tripped or len(self._held) < BLOCKED_AFTER:
            return Verdict.HELD
        self.tripped = True
        self.probes.start(now)
        return Verdict.TRIPPED

    def answered(self, issued_at: float) -> list[CrawlItem]:
        """A response other than 403, to a request sent at issued_at. Returns the held
        pages whose 403s it makes final: none, unless it settles a run."""
        if self.tripped or issued_at <= self._since:
            return []
        return self.settle()

    def settle(self) -> list[CrawlItem]:
        """End the run: returns the held pages, whose 403s are final."""
        held, self._held = self._held, []
        self._since = math.inf
        return held

    def recovered(self) -> list[CrawlItem]:
        """A probe got an answer other than 403: the block is over. Returns the held pages,
        to be tried again, once."""
        self.tripped = False
        held = self.settle()
        self._requeued.update(item.key for item in held)
        return held


def rate_ceiling(max_rps: float | None, crawl_delay: float | None) -> float | None:
    """Requests per second at most: --max-rps, or robots.txt's Crawl-delay of d seconds
    between requests, 1/d per second, whichever is lower. None if neither applies."""
    rates = [rate for rate in (max_rps, 1 / crawl_delay if crawl_delay else None) if rate]
    return min(rates, default=None)
