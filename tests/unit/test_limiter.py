"""ADR-001's concurrency law (DESIGN.md §5.3): its state machine, step by step, and its
behaviour on A.11's simulated servers, which bench/limiter_sim.py runs on this same class."""

import pytest

from tests.support.bench import simulate
from web_crawler.limiter import ConcurrencyLaw, Decision, Kind, PauseGate, rate_ceiling

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
