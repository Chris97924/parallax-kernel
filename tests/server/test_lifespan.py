"""M3-T1.4 — Tests for parallax.server.lifespan (US-011).

Covers:
1. Drain returns immediately when no inflight requests.
2. Drain waits for inflight count to reach 0.
3. Drain timeout increments counter and returns.
4. asyncio.sleep used, not time.sleep (non-blocking drain).
5. Warning logged on timeout with final inflight count.
6. Info logged on clean drain.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from parallax.router.inflight import get_inflight_count, inflight_gauge
from parallax.server.lifespan import _drain_inflight, drain_timeout_total

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_gauge() -> None:
    current = get_inflight_count()
    if current > 0:
        for _ in range(current):
            inflight_gauge.dec()
    elif current < 0:
        for _ in range(-current):
            inflight_gauge.inc()


@pytest.fixture(autouse=True)
def _clean_gauge():
    _reset_gauge()
    yield
    _reset_gauge()


def _get_drain_timeout_count() -> float:
    """Read current value of drain_timeout_total counter."""
    for metric in drain_timeout_total.collect():
        for sample in metric.samples:
            if sample.name == "parallax_drain_timeout_total_total":
                return sample.value
    # Fallback for older prometheus_client that uses plain name
    for metric in drain_timeout_total.collect():
        for sample in metric.samples:
            if not sample.name.endswith("_created"):
                return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lifespan_drains_immediately_when_no_inflight():
    """Gauge is already 0 → drain returns in 1 poll interval, no timeout increment."""
    assert get_inflight_count() == 0
    before = _get_drain_timeout_count()

    asyncio.run(_drain_inflight(timeout_seconds=1.0, poll_interval_seconds=0.05))

    after = _get_drain_timeout_count()
    assert after == before, "drain_timeout_total should not increment on clean drain"


@pytest.mark.unit
def test_lifespan_waits_for_drain():
    """Manually inc the gauge, dec after 0.1s → drain completes, no timeout."""
    inflight_gauge.inc()
    assert get_inflight_count() == 1

    before = _get_drain_timeout_count()

    async def _run():
        async def _delayed_dec():
            await asyncio.sleep(0.1)
            inflight_gauge.dec()

        await asyncio.gather(
            _drain_inflight(timeout_seconds=2.0, poll_interval_seconds=0.05),
            _delayed_dec(),
        )

    asyncio.run(_run())

    after = _get_drain_timeout_count()
    assert after == before, "drain_timeout_total should not increment on clean drain"
    assert get_inflight_count() == 0


@pytest.mark.unit
def test_lifespan_timeout_increments_counter():
    """Gauge never reaches 0 → timeout fires → counter increments."""
    inflight_gauge.inc()
    assert get_inflight_count() == 1

    before = _get_drain_timeout_count()

    asyncio.run(_drain_inflight(timeout_seconds=0.2, poll_interval_seconds=0.05))

    after = _get_drain_timeout_count()
    assert after == before + 1.0, (
        f"drain_timeout_total should increment by 1 on timeout. " f"Before={before}, after={after}"
    )


@pytest.mark.unit
def test_lifespan_uses_asyncio_sleep_not_time_sleep():
    """Verify asyncio.sleep is used (non-blocking); time.sleep is never called."""
    inflight_gauge.inc()

    asyncio_sleep_calls: list[float] = []
    time_sleep_calls: list[float] = []

    original_asyncio_sleep = asyncio.sleep

    async def _fake_asyncio_sleep(delay, *args, **kwargs):
        asyncio_sleep_calls.append(delay)
        # Actually advance time a tiny bit so drain can check the gauge
        if len(asyncio_sleep_calls) >= 2:
            # After 2 polls, dec the gauge so drain exits
            inflight_gauge.dec()
        await original_asyncio_sleep(0.001)

    def _fake_time_sleep(delay):
        time_sleep_calls.append(delay)

    with (
        patch("asyncio.sleep", side_effect=_fake_asyncio_sleep),
        patch("time.sleep", side_effect=_fake_time_sleep),
    ):
        asyncio.run(_drain_inflight(timeout_seconds=5.0, poll_interval_seconds=0.05))

    assert asyncio_sleep_calls, "asyncio.sleep should have been called"
    assert not time_sleep_calls, (
        f"time.sleep must NOT be called — it blocks the event loop. "
        f"Called with: {time_sleep_calls}"
    )


@pytest.mark.unit
def test_lifespan_logs_warning_on_timeout(caplog: pytest.LogCaptureFixture):
    """On timeout, a WARNING is emitted with the final inflight count."""
    inflight_gauge.inc()

    with caplog.at_level(logging.WARNING, logger="parallax.server.lifespan"):
        asyncio.run(_drain_inflight(timeout_seconds=0.15, poll_interval_seconds=0.05))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "Expected at least one WARNING log on drain timeout"
    # The warning should mention inflight count
    assert any(
        "1" in r.message or "inflight" in r.message.lower() for r in warnings
    ), f"Warning should mention inflight count. Got: {[r.message for r in warnings]}"


@pytest.mark.unit
def test_lifespan_logs_info_on_clean_drain(caplog: pytest.LogCaptureFixture):
    """On successful drain, an INFO log is emitted."""
    assert get_inflight_count() == 0

    with caplog.at_level(logging.INFO, logger="parallax.server.lifespan"):
        asyncio.run(_drain_inflight(timeout_seconds=1.0, poll_interval_seconds=0.05))

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert infos, "Expected at least one INFO log on clean drain"
    assert any(
        "drain" in r.message.lower() or "complete" in r.message.lower() for r in infos
    ), f"INFO log should mention drain completion. Got: {[r.message for r in infos]}"
