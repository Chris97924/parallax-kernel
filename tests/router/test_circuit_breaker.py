"""M3-T1.4 — Unit tests for parallax.router.circuit_breaker (US-011).

Covers:
- Fresh singleton is not tripped.
- Singleton identity (same object from two calls).
- Thread-safe singleton (50 threads all get same id).
- record_unreachable_observation is a no-op in stub.
- reset() clears tripped/tripped_at.
- No os.environ mutation across all public methods.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

import pytest

from parallax.router.circuit_breaker import BreakerState, get_breaker_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_singleton():
    """Reset the singleton's state before each test."""
    get_breaker_state().reset()
    yield
    get_breaker_state().reset()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_breaker_default_not_tripped():
    state = get_breaker_state()
    assert state.is_tripped() is False
    assert state.tripped is False
    assert state.tripped_at is None


@pytest.mark.unit
def test_breaker_singleton_identity():
    s1 = get_breaker_state()
    s2 = get_breaker_state()
    assert s1 is s2


@pytest.mark.unit
def test_breaker_singleton_thread_safe():
    """50 threads × 10 calls → all return the same object id."""
    ids: list[int] = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            obj_id = id(get_breaker_state())
            with lock:
                ids.append(obj_id)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == 500
    assert len(set(ids)) == 1, "All threads should get the same singleton instance."


@pytest.mark.unit
def test_record_unreachable_observation_is_noop_in_stub():
    state = get_breaker_state()
    for _ in range(20):
        state.record_unreachable_observation(observed_unreachable=True)
        state.record_unreachable_observation(observed_unreachable=False)
    # State must remain unchanged (T1.4 stub — no-op)
    assert state.is_tripped() is False
    assert state.tripped is False
    assert state.tripped_at is None


@pytest.mark.unit
def test_reset_clears_state():
    state = get_breaker_state()
    # Manually set tripped state (simulating what T1.5 would do)
    state.tripped = True
    state.tripped_at = datetime.now(tz=timezone.utc)
    assert state.is_tripped() is True
    assert state.tripped_at is not None

    state.reset()

    assert state.is_tripped() is False
    assert state.tripped is False
    assert state.tripped_at is None


@pytest.mark.unit
def test_does_not_mutate_environ():
    """None of the public methods mutate os.environ."""
    before = dict(os.environ)
    state = get_breaker_state()

    state.is_tripped()
    state.record_unreachable_observation(observed_unreachable=True)
    state.record_unreachable_observation(observed_unreachable=False)
    state.reset()
    # Manually trip and reset again
    state.tripped = True
    state.reset()

    after = dict(os.environ)
    assert before == after, (
        f"os.environ was mutated. Diff: "
        f"added={set(after) - set(before)}, "
        f"removed={set(before) - set(after)}"
    )
