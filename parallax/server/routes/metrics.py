"""GET /metrics — Prometheus text-format endpoint.

Wraps :mod:`parallax.obs.metrics` (in-house thread-safe Counter registry) and
exposes WS-3 shadow observability gauges:

* ``parallax_shadow_discrepancy_rate`` — current rolling-1h discrepancy rate
* ``parallax_shadow_checksum_consistency`` — current rolling-1h consistency
* ``parallax_shadow_log_records_total`` — record count in the rolling window

Unauthenticated by design: Prometheus scrape jobs typically don't carry bearer
tokens, and the metric values are aggregate floats with no PII or query
contents. Same posture as ``/healthz``.

Disk reads are cached for ``_CACHE_TTL_SECONDS`` so concurrent scrapes don't
re-walk the JSONL files. Tests can reset the cache via
``_reset_cache_for_tests()``.
"""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    generate_latest,
)

from parallax.obs.metrics import registry as _inhouse_registry
from parallax.shadow.discrepancy import (
    is_record_consistent,
    load_records,
    parse_window,
)

__all__ = ["router"]

router = APIRouter(tags=["meta"])

# Cache scrape results so a Prometheus 15s scrape interval doesn't flog disk.
# 30s TTL is a deliberate over-shoot so two consecutive scrapes hit the cache.
#
# Trade-off: alerting latency is bounded by `30s + scrape_interval`. With a
# 15s Prometheus scrape interval, post-incident discrepancy spikes can show
# stale healthy values for up to 30s. Acceptable for the 72h DoD window
# (30s is noise on a 72h timeline). Tighten this if sub-minute alerts ever
# matter.
_CACHE_TTL_SECONDS = 30.0
_WINDOW = "1h"

_cache_lock = threading.Lock()
_cache: dict[str, float] | None = None
_cache_at: float = 0.0


def _reset_cache_for_tests() -> None:
    """Drop the in-process cache. Test-only — never call from production code."""
    global _cache, _cache_at
    with _cache_lock:
        _cache = None
        _cache_at = 0.0


def _collect_shadow_metrics() -> dict[str, float]:
    """Compute all three shadow gauge values with a single ``load_records`` walk.

    Calling ``discrepancy_rate`` + ``checksum_consistency`` separately would
    re-walk the JSONL directory twice; collapsing here trims a cache-miss
    scrape from 3 reads to 1. Semantics must mirror the public functions
    exactly — drift is pinned by ``test_metrics_collapsed_walk_matches_*``
    in tests/server/test_metrics_endpoint.py.
    """
    delta = parse_window(_WINDOW)
    loaded = load_records(since=delta)
    parsed = len(loaded.records)
    total = parsed + loaded.malformed

    diverge = sum(1 for r in loaded.records if r.get("arbitration_outcome") == "diverge")
    discrepancy = diverge / parsed if parsed else 0.0

    if total:
        consistent = sum(
            1
            for record, raw in zip(loaded.records, loaded.raw_lines, strict=True)
            if is_record_consistent(record, raw)
        )
        consistency = consistent / total
    else:
        consistency = 1.0

    return {
        "discrepancy_rate": discrepancy,
        "checksum_consistency": consistency,
        "log_records_total": float(parsed),
    }


def _cached_shadow_metrics() -> dict[str, float]:
    """Read-then-fill cache under a single lock to prevent N concurrent scrapes
    from each running ``_collect_shadow_metrics()`` (which walks the JSONL dir).

    Holding the lock across the disk read trades scrape latency for
    correctness: a burst of N scrapes computes the metric exactly once, then
    each waiter copies the cached dict. Disk I/O time dominates lock-hold time
    only under abnormal scrape concurrency (>>1/s) — Prometheus default is
    well under that.
    """
    global _cache, _cache_at
    with _cache_lock:
        now = time.monotonic()
        if _cache is not None and (now - _cache_at) < _CACHE_TTL_SECONDS:
            return _cache
        fresh = _collect_shadow_metrics()
        _cache = fresh
        _cache_at = time.monotonic()
        return fresh


def _build_payload() -> str:
    """Render Prometheus text format combining in-house counters + shadow gauges."""
    reg = CollectorRegistry()

    # In-house counters: emit each as a Prometheus Gauge mirroring its current value.
    # Counter (monotonic) would be more idiomatic, but the existing in-house Counter
    # supports reset() (used by tests), so a Gauge mirror is the safer adapter.
    for name, counter in _inhouse_registry.items():
        gauge = Gauge(
            f"parallax_{name}",
            f"Mirror of parallax.obs.metrics.{name}",
            registry=reg,
        )
        gauge.set(counter.value)

    metrics = _cached_shadow_metrics()
    discrepancy = Gauge(
        "parallax_shadow_discrepancy_rate",
        "Fraction of arbitration_outcome=diverge records in the rolling 1h window.",
        registry=reg,
    )
    discrepancy.set(metrics["discrepancy_rate"])

    consistency = Gauge(
        "parallax_shadow_checksum_consistency",
        "Fraction of consistent (parseable, 9-field, schema-locked) records in the "
        "rolling 1h window.",
        registry=reg,
    )
    consistency.set(metrics["checksum_consistency"])

    log_count = Gauge(
        "parallax_shadow_log_records_total",
        "Parsed shadow decision-log record count in the rolling 1h window.",
        registry=reg,
    )
    log_count.set(metrics["log_records_total"])

    return generate_latest(reg).decode("utf-8")


@router.get("/metrics", response_class=PlainTextResponse)
def get_metrics() -> PlainTextResponse:
    """Prometheus scrape endpoint. Unauthenticated, no PII, aggregate values only."""
    return PlainTextResponse(_build_payload(), media_type=CONTENT_TYPE_LATEST)
