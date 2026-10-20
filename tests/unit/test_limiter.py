"""ADR-001's concurrency law (DESIGN.md §5.3): its state machine, step by step, and its
behaviour on A.11's simulated servers, which bench/limiter_sim.py runs on this same class."""

import email.utils
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.support.bench import simulate
from web_crawler.limiter import (
    BLOCKED_AFTER,
    LONGEST_PROBE,
    MAX_PAUSE,
    OPENS_AFTER,
    BlockGuard,
    CircuitBreaker,
    ConcurrencyLaw,
    Decision,
    Kind,
    PauseGate,
    Probes,
    Verdict,
    parse_retry_after,
    rate_ceiling,
)
from web_crawler.types import CrawlItem, ErrorKind

BASE = 0.100  # seconds: the first round's median time to first byte


def round_of(law: ConcurrencyLaw, ttfb: float, now: float = 0.0) -> Decision | None:
    """A whole round of samples, all ttfb, for requests issued at now."""
    decisions = [law.on_sample(ttfb, issued_at=now, now=now) for _ in range(law.limit)]
    assert decisions[:-1] == [None] * (len(decisions) - 1)
    return decisions[-1]


def in_avoidance(limit: int) -> ConcurrencyLaw:
    """A law out of slow start at limit, with its base measured: it doubles up to a ceiling
    of twice limit, then an inflated round halves it."""
    law = ConcurrencyLaw(limit * 2)
    round_of(law, BASE)
    while law.limit < law.ceiling:
        round_of(law, BASE)
    round_of(law, BASE * 2)
    assert law.limit == limit
    return law


def test_starts_at_8_in_slow_start() -> None:
    assert ConcurrencyLaw(64).limit == 8
    assert ConcurrencyLaw(3).limit == 3  # --max-concurrency is the ceiling
    with pytest.raises(ValueError, match="ceiling must be at least 1"):
        ConcurrencyLaw(0)


def test_the_first_round_measures_the_base() -> None:
    law = ConcurrencyLaw(64)
    assert round_of(law, BASE) == Decision(Kind.BASE, 8, 8, BASE, BASE)


def test_slow_start_doubles_each_round_up_to_the_ceiling() -> None:
    law = ConcurrencyLaw(50)
    round_of(law, BASE)
    assert [(round_of(law, BASE * 1.4) or Decision(Kind.HOLD, 0, 0)).limit for _ in range(4)] == [
        16,
        32,
        50,
        50,
    ]


def test_an_inflated_round_ends_slow_start_halving_the_limit() -> None:
    law = ConcurrencyLaw(64)
    round_of(law, BASE)
    round_of(law, BASE)
    assert round_of(law, BASE * 1.5) == Decision(Kind.SLOW_START_EXIT, 16, 8, BASE * 1.5, BASE)
    assert round_of(law, BASE) == Decision(Kind.GROW, 8, 9, BASE, BASE)  # one at a time now


def test_grows_by_one_below_1_5_times_the_base_and_holds_above() -> None:
    law = in_avoidance(20)
    assert round_of(law, BASE * 1.49) == Decision(Kind.GROW, 20, 21, BASE * 1.49, BASE)
    assert round_of(law, BASE * 1.5) == Decision(Kind.HOLD, 21, 21, BASE * 1.5, BASE)


def test_the_round_median_is_what_counts() -> None:
    law = in_avoidance(20)
    for ttfb in [BASE] * 10 + [BASE * 10] * 10:  # half the round is ten times slower
        decision = law.on_sample(ttfb, 0.0, 0.0)
    assert decision is not None
    assert decision.kind is Kind.HOLD
    assert decision.median == pytest.approx(BASE * 5.5)


def held(law: ConcurrencyLaw, rounds: int, ttfb: float = BASE * 2, now: float = 0.0) -> None:
    for _ in range(rounds):
        assert round_of(law, ttfb, now) == Decision(Kind.HOLD, law.limit, law.limit, ttfb, BASE)


def test_ten_held_rounds_start_a_probe_at_half_the_limit() -> None:
    law = in_avoidance(20)
    held(law, 9)
    assert round_of(law, BASE * 2, now=5.0) == Decision(Kind.PROBE, 20, 10, BASE * 2, BASE)


def test_a_round_that_isnt_held_starts_the_count_again() -> None:
    law = in_avoidance(20)
    held(law, 9)
    round_of(law, BASE)  # grows to 21
    held(law, 9)
    assert round_of(law, BASE * 2) == Decision(Kind.PROBE, 21, 10, BASE * 2, BASE)


def probing(resume: int = 20) -> ConcurrencyLaw:
    """A law that started a probe at time 5, at half of resume."""
    law = in_avoidance(resume)
    held(law, 9)
    round_of(law, BASE * 2, now=5.0)
    return law


def test_a_probe_counts_only_requests_issued_during_it() -> None:
    law = probing()
    # Responses to requests sent before the probe began say nothing about the lower load.
    assert [law.on_sample(BASE * 9, issued_at=4.9, now=5.1) for _ in range(30)] == [None] * 30
    assert round_of(law, BASE, now=5.2) is None  # its first round
    assert round_of(law, BASE, now=5.3) == Decision(Kind.PROBE_OURS, 10, 15, BASE, BASE)


def test_a_probe_that_finds_the_queueing_was_ours_resumes_at_three_quarters() -> None:
    law = probing(21)
    round_of(law, BASE * 1.6, now=6.0)
    # 80% of the held median is the most that counts as the crawl's own queueing.
    assert round_of(law, BASE * 1.6, now=6.0) == Decision(Kind.PROBE_OURS, 10, 15, BASE * 1.6, BASE)
    assert round_of(law, BASE * 1.2) == Decision(Kind.GROW, 15, 16, BASE * 1.2, BASE)


def test_a_probe_that_finds_slower_pages_takes_their_median_as_the_base() -> None:
    law = probing()
    round_of(law, BASE * 1.7, now=6.0)
    assert round_of(law, BASE * 1.7, now=6.0) == Decision(
        Kind.PROBE_NOT_OURS, 10, 20, BASE * 1.7, BASE * 1.7
    )
    # Compared with the new base, the same pages aren't inflated.
    assert round_of(law, BASE * 2) == Decision(Kind.GROW, 20, 21, BASE * 2, BASE * 1.7)


def test_congestion_halves_the_limit_and_ends_slow_start() -> None:
    law = ConcurrencyLaw(64)
    round_of(law, BASE)
    round_of(law, BASE)
    assert law.on_congestion(issued_at=1.0, now=2.0) == Decision(Kind.CUT, 16, 8)
    assert round_of(law, BASE) == Decision(Kind.GROW, 8, 9, BASE, BASE)  # not 16


def test_congestion_cuts_once_per_event() -> None:
    law = in_avoidance(40)
    assert law.on_congestion(issued_at=1.0, now=2.0) == Decision(Kind.CUT, 40, 20)
    # Requests sent before that cut are part of the same event: the cut has answered them.
    assert law.on_congestion(issued_at=1.5, now=2.1) is None
    assert law.on_congestion(issued_at=2.0, now=2.2) is None
    assert law.limit == 20
    # One sent after it is a new event.
    assert law.on_congestion(issued_at=2.05, now=2.3) == Decision(Kind.CUT, 20, 10)


def test_a_cut_discards_the_partial_round_and_cancels_a_probe() -> None:
    law = probing()
    round_of(law, BASE, now=5.5)  # the probe's first round
    for _ in range(5):
        law.on_sample(BASE, issued_at=5.6, now=5.6)
    assert law.on_congestion(issued_at=5.6, now=5.7) == Decision(Kind.CUT, 10, 5)
    # A whole round from now on: the five samples before the cut were dropped, and the probe
    # is over, so this is an ordinary round.
    for _ in range(4):
        assert law.on_sample(BASE, issued_at=5.8, now=5.8) is None
    assert law.on_sample(BASE, issued_at=5.8, now=5.8) == Decision(Kind.GROW, 5, 6, BASE, BASE)


def test_the_limit_stays_at_least_1() -> None:
    law = ConcurrencyLaw(1)
    assert law.on_congestion(0.0, 1.0) == Decision(Kind.CUT, 1, 1)
    round_of(law, BASE)
    assert round_of(law, BASE) == Decision(Kind.GROW, 1, 1, BASE, BASE)  # at the ceiling


def test_decisions_read_as_log_lines() -> None:
    assert str(Decision(Kind.GROW, 8, 16, 0.1234, 0.1)) == (
        "grow: 8 -> 16, median 123.4 ms, base 100.0 ms"
    )
    assert str(Decision(Kind.CUT, 40, 20)) == "cut: 40 -> 20"


def test_the_pause_gate_spaces_starts_evenly() -> None:
    gate = PauseGate(rate=4)  # a start every 0.25 s
    assert gate.delay(10.0) == 0
    gate.take(10.0)
    assert gate.delay(10.0) == 0.25
    assert gate.delay(10.1) == pytest.approx(0.15)
    assert gate.delay(10.3) == 0
    gate.take(10.3)  # a start that came late: the next waits its full turn, no catching up
    assert gate.delay(10.5) == pytest.approx(0.05)


def test_without_a_rate_the_gate_never_waits() -> None:
    gate = PauseGate()
    gate.take(1.0)
    assert gate.delay(1.0) == 0


def test_a_pause_holds_every_start_back_until_it_ends() -> None:
    gate = PauseGate()
    gate.pause(until=40.0)
    assert gate.delay(10.0) == 30.0
    gate.pause(until=20.0)  # an earlier end doesn't shorten it
    assert gate.delay(10.0) == 30.0
    gate.pause(until=50.0)  # a later one extends it
    assert gate.delay(10.0) == 40.0
    assert gate.delay(50.0) == 0


def test_a_pause_and_the_rate_ceiling_each_hold_starts_back() -> None:
    gate = PauseGate(rate=1)
    gate.take(10.0)
    gate.pause(until=10.5)
    assert gate.delay(10.0) == 1.0  # the rate's turn is later than the pause's end
    gate.pause(until=12.0)
    assert gate.delay(10.0) == 2.0
    assert gate.delay(12.0) == 0


NOW = 784111777.0  # Sun, 06 Nov 1994 08:49:37 GMT, RFC 9110's example date


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("120", 120.0),
        ("0", 0.0),
        (" 5\t", 5.0),
        ("300", MAX_PAUSE),
        ("301", MAX_PAUSE),  # capped
        ("9" * 400, MAX_PAUSE),
        # An HTTP date, in each of the three formats HTTP allows: IMF-fixdate, RFC 850 and
        # asctime, which has no zone and is GMT.
        ("Sun, 06 Nov 1994 08:51:37 GMT", 120.0),
        ("Sunday, 06-Nov-94 08:51:37 GMT", 120.0),
        ("Sun Nov  6 08:51:37 1994", 120.0),
        ("Sun, 06 Nov 1994 08:49:37 GMT", 0.0),  # now
        ("Sun, 06 Nov 1994 08:00:00 GMT", 0.0),  # past
        ("Sun, 06 Nov 1994 09:49:37 GMT", MAX_PAUSE),  # an hour from now: capped
    ],
)
def test_retry_after_is_seconds_or_a_date(value: str, seconds: float) -> None:
    assert parse_retry_after(value, NOW) == seconds


@pytest.mark.parametrize(
    "value",
    [None, "", "soon", "-5", "1.5", "+30", "١٢٠", "1994-11-06T08:51:37Z", "Sun, 31 Feb 1994"],
)
def test_retry_after_that_says_neither_is_ignored(value: str | None) -> None:
    assert parse_retry_after(value, NOW) is None


@given(st.text(), st.floats(0, 4e9))
def test_retry_after_is_never_more_than_the_cap(value: str, now: float) -> None:
    seconds = parse_retry_after(value, now)
    assert seconds is None or 0.0 <= seconds <= MAX_PAUSE


@given(st.integers(0, 10**6))
def test_retry_after_reads_a_date_that_email_utils_writes(ahead: int) -> None:
    date = email.utils.formatdate(NOW + ahead, usegmt=True)
    assert parse_retry_after(date, NOW) == min(ahead, MAX_PAUSE)
    assert math.isclose(
        parse_retry_after(date, NOW + 0.25) or 0.0, max(min(ahead, 300.25) - 0.25, 0.0)
    )


@pytest.mark.parametrize(
    ("max_rps", "crawl_delay", "rate"),
    [
        (None, None, None),
        (10.0, None, 10.0),
        (None, 2.0, 0.5),  # Crawl-delay: 2, a request every 2 s
        (10.0, 2.0, 0.5),  # whichever allows fewer
        (0.25, 2.0, 0.25),
        (None, 0.0, None),  # Crawl-delay: 0 asks for nothing
    ],
)
def test_rate_ceiling(max_rps: float | None, crawl_delay: float | None, rate: float | None) -> None:
    assert rate_ceiling(max_rps, crawl_delay) == rate


def test_probes_back_off_from_the_first_wait_to_a_minute() -> None:
    probes = Probes(first=1.0, give_up_after=300.0)
    probes.start(100.0)
    assert (probes.began, probes.next, probes.deadline) == (100.0, 101.0, 400.0)
    waits = []
    for _ in range(9):
        now = probes.next
        probes.failed(now)
        waits.append(probes.next - now)
    assert waits == [2.0, 4.0, 8.0, 16.0, 32.0, LONGEST_PROBE, 60.0, 60.0, 60.0]
    probes.start(1000.0)  # a pause later on starts from the first wait again
    assert (probes.next, probes.deadline) == (1001.0, 1300.0)


def opened(breaker: CircuitBreaker, at: float = 10.0) -> CircuitBreaker:
    """breaker, opened by failures of requests sent up to time at."""
    for n in range(OPENS_AFTER):
        breaker.failed(ErrorKind.CONNECT, issued_at=at - OPENS_AFTER + n + 1, now=at)
    assert breaker.open
    return breaker


@pytest.mark.parametrize(
    "kind", [ErrorKind.DNS, ErrorKind.CONNECT, ErrorKind.TLS, ErrorKind.CONNECT_TIMEOUT]
)
def test_the_breaker_opens_after_20_connection_failures_in_a_row(kind: ErrorKind) -> None:
    breaker = CircuitBreaker(first_probe=1.0, give_up_after=300.0)
    for n in range(OPENS_AFTER - 1):
        assert not breaker.failed(kind, issued_at=n, now=n + 0.5)
        assert not breaker.open
    assert breaker.failed(kind, issued_at=19.0, now=19.5)
    assert breaker.open
    assert breaker.last is kind
    assert (breaker.probes.next, breaker.probes.deadline) == (20.5, 319.5)


@pytest.mark.parametrize(
    "kind",
    [
        ErrorKind.RESET,
        ErrorKind.TIMEOUT,
        ErrorKind.PAYLOAD,
        ErrorKind.PROTOCOL,
        ErrorKind.INVALID_URL,
    ],
)
def test_a_failure_after_connecting_neither_counts_nor_breaks_a_run(kind: ErrorKind) -> None:
    breaker = CircuitBreaker()
    for n in range(OPENS_AFTER - 1):
        breaker.failed(ErrorKind.CONNECT, issued_at=n, now=n)
        assert not breaker.failed(kind, issued_at=n, now=n)
    assert breaker.failed(ErrorKind.CONNECT, issued_at=19.0, now=19.0)


def test_a_response_starts_the_count_again() -> None:
    breaker = CircuitBreaker()
    for n in range(OPENS_AFTER - 1):
        breaker.failed(ErrorKind.CONNECT, issued_at=n, now=n)
    assert not breaker.answered(issued_at=18.5)  # sent after the last failure's request
    for n in range(OPENS_AFTER - 1):
        assert not breaker.failed(ErrorKind.CONNECT, issued_at=20 + n, now=20 + n)
    assert not breaker.open


def test_a_response_to_a_request_sent_before_the_failures_says_nothing() -> None:
    breaker = CircuitBreaker()
    for n in range(OPENS_AFTER - 1):
        breaker.failed(ErrorKind.CONNECT, issued_at=n, now=n)
    assert not breaker.answered(issued_at=18.0)  # sent with the last failure
    assert breaker.failed(ErrorKind.CONNECT, issued_at=19.0, now=19.0)
    assert not breaker.answered(issued_at=5.0)  # long on its way: the host may be gone since
    assert breaker.open


def test_a_response_to_a_later_request_closes_the_breaker() -> None:
    breaker = opened(CircuitBreaker())
    assert breaker.answered(issued_at=10.5)  # a probe
    assert not breaker.open
    assert not breaker.answered(issued_at=11.0)  # already closed
    # Closed, it counts afresh, and a run opens it again, with probes from the first wait.
    for n in range(OPENS_AFTER - 1):
        breaker.failed(ErrorKind.DNS, issued_at=50 + n, now=50 + n)
    assert breaker.failed(ErrorKind.DNS, issued_at=69.0, now=69.5)
    assert breaker.probes.next == 70.5


def test_failures_while_open_change_nothing() -> None:
    breaker = opened(CircuitBreaker())
    probes = (breaker.probes.next, breaker.probes.deadline)
    assert not breaker.failed(ErrorKind.TLS, issued_at=10.0, now=12.0)
    assert breaker.last is ErrorKind.CONNECT
    assert (breaker.probes.next, breaker.probes.deadline) == probes
    assert breaker.open


def page(n: int) -> CrawlItem:
    return CrawlItem(f"https://example.com/{n}", f"https://example.com/{n}", 1)


def test_403s_are_held_while_their_run_is_short_of_a_block() -> None:
    guard = BlockGuard()
    for n in range(BLOCKED_AFTER - 1):
        assert guard.forbidden(page(n), issued_at=n, now=n) is Verdict.HELD
    assert guard.held == BLOCKED_AFTER - 1
    assert not guard.tripped


def test_a_response_to_a_request_sent_after_the_runs_first_403_settles_it() -> None:
    guard = BlockGuard()
    guard.forbidden(page(0), issued_at=5.0, now=6.0)
    guard.forbidden(page(1), issued_at=7.0, now=8.0)
    assert guard.answered(issued_at=4.0) == []  # sent before the run began: may be stale
    assert guard.answered(issued_at=5.0) == []
    assert guard.answered(issued_at=5.5) == [page(0), page(1)]  # the host serves the crawl
    assert guard.held == 0
    assert guard.answered(issued_at=100.0) == []  # no run to settle


def test_a_run_begins_with_its_earliest_request() -> None:
    # A 403 for a request sent earlier, though it came later, moves the run's start back.
    guard = BlockGuard()
    guard.forbidden(page(0), issued_at=5.0, now=6.0)
    guard.forbidden(page(1), issued_at=3.0, now=7.0)
    assert guard.answered(issued_at=4.0) == [page(0), page(1)]


def tripped(guard: BlockGuard, now: float = 20.0) -> BlockGuard:
    for n in range(BLOCKED_AFTER - 1):
        assert guard.forbidden(page(n), issued_at=n, now=n) is Verdict.HELD
    assert guard.forbidden(page(19), issued_at=19.0, now=now) is Verdict.TRIPPED
    return guard


def test_the_20th_403_in_a_row_trips_the_guard() -> None:
    guard = tripped(BlockGuard(first_probe=1.0, give_up_after=300.0))
    assert guard.tripped
    assert (guard.probes.next, guard.probes.deadline) == (21.0, 320.0)
    # Tripped, it holds every 403, and only a probe ends it.
    assert guard.forbidden(page(20), issued_at=18.0, now=21.0) is Verdict.HELD
    assert guard.answered(issued_at=25.0) == []
    assert guard.tripped
    assert guard.held == BLOCKED_AFTER + 1


def test_when_the_block_ends_the_held_pages_are_tried_again_once() -> None:
    guard = tripped(BlockGuard())
    assert guard.recovered() == [page(n) for n in range(BLOCKED_AFTER)]
    assert not guard.tripped
    assert guard.held == 0
    # A page tried again that gets another 403 is forbidden; other pages are held again.
    assert guard.forbidden(page(3), issued_at=30.0, now=31.0) is Verdict.FINAL
    assert guard.forbidden(page(99), issued_at=30.0, now=31.0) is Verdict.HELD
    assert guard.held == 1


def test_settling_makes_the_held_403s_final() -> None:
    guard = BlockGuard()
    for n in range(3):
        guard.forbidden(page(n), issued_at=n, now=n)
    assert guard.settle() == [page(0), page(1), page(2)]
    assert guard.settle() == []
    # A new run starts afresh: a response sent after the old run's start doesn't settle it.
    guard.forbidden(page(5), issued_at=10.0, now=10.0)
    assert guard.answered(issued_at=5.0) == []


SEEDS = (1, 2, 3)


@pytest.mark.parametrize("scenario", ["S0.4", "S0.8", "A", "A2"])
@pytest.mark.parametrize("seed", SEEDS)
def test_with_spare_capacity_it_crawls_about_as_fast_as_errors_alone(
    scenario: str, seed: int
) -> None:
    # ADR-001's bar: within 15% of the law that only errors cut.
    production = simulate("production", scenario, seed)
    errors = simulate("errors", scenario, seed)
    assert production.time <= 1.15 * errors.time


@pytest.mark.parametrize("seed", SEEDS)
def test_on_a_small_origin_it_keeps_queueing_under_half_of_errors_alone(seed: int) -> None:
    production = simulate("production", "B", seed)
    errors = simulate("errors", "B", seed)
    assert production.queued < 0.5 * errors.queued
    assert production.time <= 1.05 * errors.time  # for 1-2% more crawl time


@pytest.mark.parametrize("seed", SEEDS)
def test_on_a_throttling_origin_it_cuts_once_per_burst_of_429s(seed: int) -> None:
    # An origin that answers 429 to a request arriving while 20 others are being served.
    run = simulate("production", "T", seed)
    cuts = [decision for decision in run.decisions if decision.kind is Kind.CUT]
    # Slow start overshoots to 32, and a dozen requests get a 429: one cut answers them all.
    assert (cuts[0].previous, cuts[0].limit) == (32, 16)
    assert run.throttled > len(cuts)
    after = run.decisions[run.decisions.index(cuts[0]) :]
    # From then on, the limit climbs back to about 20 and is halved again, over and over:
    # AIMD's sawtooth, never cascading below half of what the origin takes.
    lowest = min(decision.limit for decision in after)
    assert lowest >= 10
    assert max(decision.limit for decision in after) <= 24
    assert 14 < run.mean_limit < 18
    assert run.throttled < 0.01 * 20_000
    # Cutting for every 429 lets one burst halve the limit again and again.
    every = simulate("every cut", "T", seed)
    assert min(decision.limit for decision in every.decisions) < lowest
