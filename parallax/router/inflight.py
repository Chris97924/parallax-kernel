"""M3-T1.4 — In-process inflight request gauge (US-011).

Tracks the count of active dual-read-aware HTTP requests for the graceful
drain procedure.  The gauge is backed by prometheus_client so it's visible
at the ``/metrics`` scrape endpoint without extra wiring.

Dependency note: ``inflight_gauge._value.get()`` uses a private attribute
of ``prometheus_client.Gauge``.  This is stable across all 0.x versions of
the library (the attribute has existed since 0.7.x and is not under an
experimental flag).  If the library ever removes it, update
``get_inflight_count`` to parse ``generate_latest`` instead.
"""

from __future__ import annotations

from typing import Any

import prometheus_client

__all__ = ["inflight_gauge", "InflightTracker", "get_inflight_count"]


def _get_or_create_gauge(name: str, doc: str) -> prometheus_client.Gauge:
    """Return an existing Gauge or create a new one.

    prometheus_client raises ``ValueError`` when you register the same name
    twice (e.g. on module re-import in a test suite that re-imports the
    module).  The try/except pattern mirrors the convention in
    :mod:`parallax.router.sqlite_gate`.
    """
    try:
        return prometheus_client.Gauge(name, doc)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


inflight_gauge: prometheus_client.Gauge = _get_or_create_gauge(
    "parallax_inflight_requests",
    "Number of HTTP requests currently being processed (dual-read-aware path).",
)


def get_inflight_count() -> int:
    """Return the current inflight request count as a plain int.

    Reads ``inflight_gauge._value.get()`` which returns a ``float`` in
    prometheus_client 0.x; we cast to ``int`` for drain-loop comparisons.
    """
    return int(inflight_gauge._value.get())  # type: ignore[attr-defined]


class InflightTracker:
    """Context manager that increments the inflight gauge on enter and
    decrements it on exit — even when the body raises an exception.

    Thread-safe: prometheus_client's underlying ``_value`` uses an internal
    lock; no additional lock is needed here.

    Usage::

        with InflightTracker():
            response = await call_next(request)
    """

    __slots__ = ()

    def __enter__(self) -> InflightTracker:
        """Increment the inflight gauge and return self."""
        inflight_gauge.inc()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Decrement the inflight gauge; does not suppress exceptions."""
        inflight_gauge.dec()
        # Return None (falsy) → exception propagates unchanged.
