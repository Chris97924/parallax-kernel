"""M3-T1.4 — Graceful drain lifespan handler (US-011).

Implements the rollback drain contract from ralplan §3 M3-T1.4:
on SIGTERM / SIGINT the server waits up to ``DRAIN_TIMEOUT_SECONDS`` (15 min)
for all in-flight requests to complete before the process exits.

Without this handler the rollback procedure "drain in-flight 15 min" is
paper — in-flight requests would crash on a ``DUAL_READ=false`` flip during
a live rollback.

Uses FastAPI's ``lifespan`` context-manager pattern (0.93+).  The deprecated
``@app.on_event("startup"/"shutdown")`` decorators are NOT used.

Important: the drain loop uses ``asyncio.sleep`` (not ``time.sleep``) so
other coroutines — including the last in-flight requests — can progress
while we poll.  Using ``time.sleep`` would block the event loop and make
it impossible for those requests to actually finish.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Final

from fastapi import FastAPI
from prometheus_client import Counter

from parallax.router.inflight import get_inflight_count

__all__ = [
    "DRAIN_TIMEOUT_SECONDS",
    "DRAIN_POLL_INTERVAL_SECONDS",
    "drain_timeout_total",
    "parallax_lifespan",
]

_log = logging.getLogger("parallax.server.lifespan")

DRAIN_TIMEOUT_SECONDS: Final[float] = 900.0  # 15 minutes
DRAIN_POLL_INTERVAL_SECONDS: Final[float] = 0.5


def _get_or_create_counter(name: str, doc: str) -> Counter:
    """Return an existing Counter or create a new one (re-import safe)."""
    try:
        return Counter(name, doc)
    except ValueError:
        return Counter.__class__.__new__(Counter)  # type: ignore[misc]  # fallback


# prometheus_client raises ValueError on duplicate registration.
try:
    drain_timeout_total: Counter = Counter(
        "parallax_drain_timeout_total",
        "Number of times the graceful-drain timeout fired before all "
        "in-flight requests completed.",
    )
except ValueError:
    import prometheus_client as _pc

    drain_timeout_total = _pc.REGISTRY._names_to_collectors["parallax_drain_timeout_total"]  # type: ignore[assignment]


async def _drain_inflight(
    *,
    timeout_seconds: float = DRAIN_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DRAIN_POLL_INTERVAL_SECONDS,
) -> None:
    """Poll until inflight count reaches 0 or *timeout_seconds* elapses.

    On timeout:
    - Increments ``drain_timeout_total`` Prometheus counter.
    - Logs a WARNING with the final inflight count.
    - Returns (lifespan exits regardless; we do not block forever).

    On clean drain:
    - Logs an INFO message with elapsed time.

    Parameters
    ----------
    timeout_seconds:
        Maximum seconds to wait.  Pass a small value (e.g. 0.2) in tests.
    poll_interval_seconds:
        How often to check the gauge.
    """
    start = time.monotonic()
    deadline = start + timeout_seconds

    while True:
        count = get_inflight_count()
        if count <= 0:
            elapsed = time.monotonic() - start
            _log.info("parallax.lifespan: drain complete in %.3fs (0 inflight)", elapsed)
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            final_count = get_inflight_count()
            drain_timeout_total.inc()
            _log.warning(
                "parallax.lifespan: drain timeout after %.1fs — %d request(s) still "
                "in flight; proceeding with shutdown",
                timeout_seconds,
                final_count,
            )
            return

        await asyncio.sleep(min(poll_interval_seconds, max(remaining, 0)))


@contextlib.asynccontextmanager
async def parallax_lifespan(app: FastAPI):  # type: ignore[type-arg]
    """FastAPI lifespan context manager.

    Startup: nothing special (future migrations / connection-pool warming
    can go here).

    Shutdown: drain in-flight requests up to ``DRAIN_TIMEOUT_SECONDS``.
    """
    # Startup
    yield
    # Shutdown — drain
    await _drain_inflight(timeout_seconds=DRAIN_TIMEOUT_SECONDS)
