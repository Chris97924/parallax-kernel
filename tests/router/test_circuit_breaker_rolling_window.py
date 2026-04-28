"""M3-T1.5 — Unit tests for circuit-breaker rolling-window logic (US-011).

Covers:
- Math: threshold, min-observations, rate calculations
- Window eviction via monkeypatched time.monotonic
- Reset: clears state, counter monotonic, re-trip after reset
- Concurrency: observation count, atomic trip transition
- Singleton consistency
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY

from parallax.router.circuit_breaker import (
    MIN_OBSERVATIONS,
    TRIP_THRESHOLD,
    WINDOW_SECONDS,
    BreakerState,
    get_breaker_state,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_state() -> BreakerState:
    """Return a fresh BreakerState (not the singleton) for isolated tests."""
    return BreakerState()


def _trip_state(state: BreakerState, *, total: int = 200, unreachable: int = 50) -> None:
    """Record enough observations to trip the breaker (default: 50/200 = 25%)."""
    assert unreachable / total > TRIP_THRESHOLD
    assert total >= MIN_OBSERVATIONS
    for i in range(total):
        state.record_unreachable_observation(observed_unreachable=(i < unreachable))


def _counter_value() -> float:
    """Read the current value of parallax_circuit_breaker_tripped_total.

    prometheus_client stores Counter metrics with the base name (without _total)
    as metric.name; the _total suffix appears only in sample names.
    """
    for metric in REGISTRY.collect():
        if metric.name == "parallax_circuit_breaker_tripped":
            for sample in metric.samples:
                if sample.name == "parallax_circuit_breaker_tripped_total":
                    return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_singleton():
    """Reset singleton before/after each test."""
    get_breaker_state().reset()
    yield
    get_breaker_state().reset()


# ---------------------------------------------------------------------------
# Math tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_trip_below_threshold():
    """50 reachable, 0 unreachable → rate=0%, is_tripped=False."""
    state = _fresh_state()
    for _ in range(50):
        state.record_unreachable_observation(observed_unreachable=False)
    assert state.is_tripped() is False
    assert state.current_unreachable_rate() == 0.0


@pytest.mark.unit
def test_no_trip_at_exactly_one_percent():
    """100 obs, 1 unreachable → rate=0.01, strict > means NOT tripped.

    IMPORTANT ordering: record all 99 reachable obs first, then the 1 unreachable
    last. This prevents an early trip at the 50th observation (1/50=2% > 1%).
    With reachable obs first, at obs 50 rate=0/50=0%, and at obs 100 rate=1/100=1%
    which is NOT > 1% (strict inequality). See T1.5 docstring.
    """
    state = _fresh_state()
    for _ in range(99):
        state.record_unreachable_observation(observed_unreachable=False)
    state.record_unreachable_observation(observed_unreachable=True)
    rate = state.current_unreachable_rate()
    assert rate == pytest.approx(0.01)
    # TRIP_THRESHOLD = 0.01 → strict > → 0.01 is NOT > 0.01
    assert rate is not None and rate <= TRIP_THRESHOLD
    assert state.is_tripped() is False


@pytest.mark.unit
def test_trips_at_one_point_five_percent():
    """200 obs, 3 unreachable → rate=0.015 > 0.01 → tripped, counter +1.

    Ordering: 197 reachable first, then 3 unreachable last. This prevents
    an early trip (if 3 unreachable arrived first, 3/50=6% at obs 50 would trip).
    At obs 200: 3/200=1.5% > 1% → trips on the last observation.
    """
    state = _fresh_state()
    before = _counter_value()
    # 197 reachable first, then 3 unreachable
    for _ in range(197):
        state.record_unreachable_observation(observed_unreachable=False)
    for _ in range(3):
        state.record_unreachable_observation(observed_unreachable=True)
    assert state.is_tripped() is True
    assert state.tripped_at is not None
    assert _counter_value() == before + 1


@pytest.mark.unit
def test_trips_at_99_percent():
    """100 obs, 99 unreachable → definitely trips."""
    state = _fresh_state()
    before = _counter_value()
    for i in range(100):
        state.record_unreachable_observation(observed_unreachable=(i < 99))
    assert state.is_tripped() is True
    assert _counter_value() == before + 1


@pytest.mark.unit
def test_does_not_trip_below_min_observations():
    """5 unreachable / 5 total = 100% but total < MIN_OBSERVATIONS → not tripped."""
    state = _fresh_state()
    for _ in range(5):
        state.record_unreachable_observation(observed_unreachable=True)
    assert state.is_tripped() is False
    # Rate is None because total < MIN_OBSERVATIONS
    assert state.current_unreachable_rate() is None
    assert state.observation_count() == 5


@pytest.mark.unit
def test_trip_transition_increments_counter_once():
    """Recording many unreachable obs → counter incremented by exactly 1."""
    state = _fresh_state()
    before = _counter_value()
    # 200 obs, 50 unreachable (25% >> 1%)
    for i in range(200):
        state.record_unreachable_observation(observed_unreachable=(i < 50))
    assert state.is_tripped() is True
    assert _counter_value() == before + 1


@pytest.mark.unit
def test_already_tripped_no_double_count():
    """Once tripped, additional unreachable obs don't increment counter again."""
    state = _fresh_state()
    # Trip the breaker
    for i in range(200):
        state.record_unreachable_observation(observed_unreachable=(i < 50))
    assert state.is_tripped() is True
    counter_after_trip = _counter_value()

    # Record 100 more unreachable
    for _ in range(100):
        state.record_unreachable_observation(observed_unreachable=True)
    assert _counter_value() == counter_after_trip  # no change


@pytest.mark.unit
def test_no_auto_recovery():
    """Tripped breaker stays tripped even after recording many reachable obs."""
    state = _fresh_state()
    # Trip it
    for i in range(200):
        state.record_unreachable_observation(observed_unreachable=(i < 50))
    assert state.is_tripped() is True

    # Flood with 1000 reachable observations
    for _ in range(1000):
        state.record_unreachable_observation(observed_unreachable=False)
    assert state.is_tripped() is True  # still tripped — manual reset only


# ---------------------------------------------------------------------------
# Window eviction tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_old_observations_evicted():
    """Observations older than WINDOW_SECONDS are evicted; rate reflects only recent ones.

    Strategy: record 100 reachable obs at base_time (these become old), then
    advance time past the window and record 50 unreachable obs.  Without eviction,
    rate = 50/150 ≈ 33%.  With eviction, rate = 50/50 = 100%.  We check that the
    old reachable obs do NOT dilute the new unreachable rate — proving eviction works.
    """
    state = _fresh_state()
    base_time = 1000.0

    # Record 100 reachable at the same timestamp (will all be evicted together)
    for _ in range(100):
        with patch("time.monotonic", return_value=base_time):
            state.record_unreachable_observation(observed_unreachable=False)

    # Advance time past the window (cutoff = future_time - 300 > base_time)
    future_time = base_time + WINDOW_SECONDS + 10.0

    # Record 50 unreachable in the new window — old 100 should be evicted
    for i in range(50):
        with patch("time.monotonic", return_value=future_time + i * 0.1):
            state.record_unreachable_observation(observed_unreachable=True)

    with patch("time.monotonic", return_value=future_time + 6.0):
        rate = state.current_unreachable_rate()

    # If eviction worked: only 50 obs in window, all unreachable → rate=1.0
    # If eviction failed: 150 obs, 50 unreachable → rate≈0.333
    assert rate == pytest.approx(1.0), (
        f"Expected 1.0 (only recent obs counted) but got {rate}. "
        "Old observations were not evicted correctly."
    )
    # Breaker is tripped because 50/50=100% >> 1%
    assert state.is_tripped() is True


@pytest.mark.unit
def test_observation_count_reflects_window():
    """After old obs evicted, observation_count reflects only recent ones.

    All 200 old observations are placed at exactly base_time (same timestamp).
    We then advance to base_time + WINDOW_SECONDS + 5 so the cutoff is
    base_time + 5, which is after base_time → all 200 are evicted.
    """
    state = _fresh_state()
    base_time = 2000.0

    # Record 200 old observations at a single fixed timestamp
    for _ in range(200):
        with patch("time.monotonic", return_value=base_time):
            state.record_unreachable_observation(observed_unreachable=False)

    # Advance past window and record 50 new ones
    future_time = base_time + WINDOW_SECONDS + 5.0
    for i in range(50):
        with patch("time.monotonic", return_value=future_time + i * 0.01):
            state.record_unreachable_observation(observed_unreachable=False)

    with patch("time.monotonic", return_value=future_time + 1.0):
        count = state.observation_count()

    assert count == 50


@pytest.mark.unit
def test_decay_does_not_auto_reset_tripped_flag():
    """Trip the breaker, then evict all observations → is_tripped() still True."""
    state = _fresh_state()
    base_time = 3000.0

    # Trip the breaker: 197 reachable + 3 unreachable, all at base_time
    for _ in range(197):
        with patch("time.monotonic", return_value=base_time):
            state.record_unreachable_observation(observed_unreachable=False)
    for _ in range(3):
        with patch("time.monotonic", return_value=base_time):
            state.record_unreachable_observation(observed_unreachable=True)

    assert state.is_tripped() is True

    # Advance time way past the window so all observations are evicted
    future_time = base_time + WINDOW_SECONDS + 100.0
    with patch("time.monotonic", return_value=future_time):
        count = state.observation_count()

    assert count == 0
    # Breaker is still tripped — decay does NOT auto-reset (Q10 line 553)
    assert state.is_tripped() is True


# ---------------------------------------------------------------------------
# Reset tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reset_clears_tripped_flag():
    """After trip + reset: is_tripped=False, tripped_at=None, observations empty."""
    state = _fresh_state()
    _trip_state(state)
    assert state.is_tripped() is True

    state.reset()

    assert state.is_tripped() is False
    assert state.tripped is False
    assert state.tripped_at is None
    assert state.observation_count() == 0


@pytest.mark.unit
def test_reset_does_not_decrement_counter():
    """Prometheus Counter is monotonic — reset() must not change its value."""
    state = _fresh_state()
    _trip_state(state)
    counter_after_trip = _counter_value()
    assert counter_after_trip > 0

    state.reset()

    # Counter must be unchanged (monotonic; reset does not undo trip events)
    assert _counter_value() == counter_after_trip


@pytest.mark.unit
def test_can_re_trip_after_reset():
    """After reset, a fresh batch of bad observations can trip the breaker again."""
    state = _fresh_state()
    _trip_state(state)
    counter_after_first_trip = _counter_value()
    state.reset()
    assert state.is_tripped() is False

    # Trip it again
    _trip_state(state)
    assert state.is_tripped() is True
    assert _counter_value() == counter_after_first_trip + 1


# ---------------------------------------------------------------------------
# Concurrency tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_concurrent_observations_no_lost_count():
    """50 threads × 10 observations = 500 in the window."""
    state = _fresh_state()
    threads = [
        threading.Thread(
            target=lambda: [
                state.record_unreachable_observation(observed_unreachable=False) for _ in range(10)
            ]
        )
        for _ in range(50)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state.observation_count() == 500


@pytest.mark.unit
def test_concurrent_trip_transition_atomic():
    """Many threads pushing bad obs near threshold → counter incremented exactly once."""
    state = _fresh_state()
    before = _counter_value()

    # Pre-load exactly MIN_OBSERVATIONS - 1 observations so the next write trips
    for _ in range(MIN_OBSERVATIONS - 1):
        state.record_unreachable_observation(observed_unreachable=True)

    # Now 50 threads simultaneously push the last observation over the threshold
    def push_bad():
        state.record_unreachable_observation(observed_unreachable=True)

    threads = [threading.Thread(target=push_bad) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state.is_tripped() is True
    # Counter must have incremented exactly once — no double-counting
    assert _counter_value() == before + 1


# ---------------------------------------------------------------------------
# Singleton consistency
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_breaker_state_returns_same_singleton_post_t15():
    """get_breaker_state() × 100 calls → all return the same object id."""
    ids = [id(get_breaker_state()) for _ in range(100)]
    assert len(set(ids)) == 1, "Multiple singleton instances detected"
