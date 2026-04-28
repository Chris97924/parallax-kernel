"""M3-T1.2 — DualReadRouter flag-gated dual-read wrapper (US-011).

Mirrors ``parallax/router/shadow.py:111-198`` (ShadowInterceptor decorator
pattern, Q7 decision: keep parallel, do NOT modify shadow.py).

Hard invariants — never violated:
1. ``DualReadResult.primary`` always set to canonical result, even on failures.
2. If flag is off (or dual_read_override=False): return immediately with
   ``outcome="skipped"``, no Aphelion call, zero overhead.
3. Q5 CHANGE_TRACE.legacy_kind=bug short-circuit: outcome="skipped", no secondary.
4. Fail-closed: any secondary exception → outcome="aphelion_unreachable" or
   "primary_only"; primary still returned.
5. ``_hits_equal`` imported from shadow.py (not copied).
6. Outcome classification: match / diverge / aphelion_unreachable / primary_only / skipped.
7. Parallel dispatch via ThreadPoolExecutor(max_workers=2) with secondary timeout.
8. Live counter integration (optional, None-safe).
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING

from parallax.obs.log import get_logger
from parallax.router.aphelion_stub import AphelionUnreachableError
from parallax.router.config import is_dual_read_enabled
from parallax.router.contracts import DualReadResult, QueryRequest
from parallax.router.discrepancy_live import (
    DualReadOutcome,
    record_dual_read_outcome,
)
from parallax.router.ports import QueryPort

# Import _hits_equal from shadow.py directly (Q7: reuse, do NOT copy-paste).
# Importing shadow.py is safe: it has no module-level side-effects that would
# break existing tests — its only side-effects are creating a log instance and
# reading SHADOW_LOG_DIR from env (done inside ShadowInterceptor.__init__, not
# at module scope).
from parallax.router.shadow import _hits_equal
from parallax.router.types import QueryType

if TYPE_CHECKING:
    from parallax.router.discrepancy_live import LiveDiscrepancyCounter

__all__ = ["DualReadRouter"]

_log = get_logger("parallax.router.dual_read")


class DualReadRouter:
    """Flag-gated dual-read wrapper. Fail-closed to canonical on any failure.

    ``primary`` is the canonical QueryPort (e.g. RealMemoryRouter).
    ``secondary`` is the Aphelion adapter (e.g. AphelionReadAdapter stub).

    The ``dual_read_override`` parameter on ``query()`` allows per-request
    snapshots (T1.4 middleware) to override the env flag without re-reading env
    mid-request.  Pass ``True`` to force-enable, ``False`` to force-disable,
    or ``None`` to fall back to ``is_dual_read_enabled()``.
    """

    def __init__(
        self,
        *,
        primary: QueryPort,
        secondary: QueryPort,
        live_counter: LiveDiscrepancyCounter | None = None,
        executor: ThreadPoolExecutor | None = None,
        secondary_timeout_ms: float = 100.0,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._live_counter = live_counter
        self._executor = executor or ThreadPoolExecutor(max_workers=2)
        self._secondary_timeout_ms = secondary_timeout_ms

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(
        self,
        request: QueryRequest,
        *,
        correlation_id: str | None = None,
        dual_read_override: bool | None = None,
    ) -> DualReadResult:
        """Dispatch primary + secondary in parallel; return DualReadResult.

        Invariant: always returns a result with ``primary`` set.  Secondary
        failures are caught and classified — they never propagate to the caller.
        Primary failures DO propagate (fail-closed in the canonical direction).
        """
        cid = correlation_id if correlation_id is not None else str(uuid.uuid4())

        # ------------------------------------------------------------------
        # Fast-path: flag off → skipped (zero overhead beyond 2 bool checks)
        # ------------------------------------------------------------------
        enabled = dual_read_override if dual_read_override is not None else is_dual_read_enabled()
        if not enabled:
            primary_start = time.perf_counter()
            primary_result = self._primary.query(request)
            latency_primary_ms = (time.perf_counter() - primary_start) * 1000.0
            result = DualReadResult(
                outcome="skipped",
                primary=primary_result,
                secondary=None,
                correlation_id=cid,
                latency_primary_ms=latency_primary_ms,
                latency_secondary_ms=None,
                aphelion_unreachable_reason=None,
            )
            self._record(request.user_id, result.outcome)
            return result

        # ------------------------------------------------------------------
        # Q5 short-circuit: CHANGE_TRACE.legacy_kind=bug → skipped
        # ------------------------------------------------------------------
        if (
            request.query_type == QueryType.CHANGE_TRACE
            and request.params is not None
            and request.params.get("legacy_kind") == "bug"
        ):
            primary_start = time.perf_counter()
            primary_result = self._primary.query(request)
            latency_primary_ms = (time.perf_counter() - primary_start) * 1000.0
            result = DualReadResult(
                outcome="skipped",
                primary=primary_result,
                secondary=None,
                correlation_id=cid,
                latency_primary_ms=latency_primary_ms,
                latency_secondary_ms=None,
                aphelion_unreachable_reason=None,
            )
            self._record(request.user_id, result.outcome)
            return result

        # ------------------------------------------------------------------
        # Dual dispatch: primary + secondary in parallel
        # ------------------------------------------------------------------
        primary_start = time.perf_counter()
        primary_future: Future[object] = self._executor.submit(self._primary.query, request)
        secondary_future: Future[object] = self._executor.submit(self._secondary.query, request)

        # Always await primary to completion — fail-closed means we must have
        # the canonical result even if secondary times out.
        try:
            primary_result = primary_future.result()
        except Exception:
            # Primary failure propagates — don't swallow it.
            secondary_future.cancel()
            raise

        latency_primary_ms = (time.perf_counter() - primary_start) * 1000.0

        # Wait for secondary up to timeout.
        secondary_timeout_s = self._secondary_timeout_ms / 1000.0
        secondary_start = time.perf_counter()
        done, not_done = wait([secondary_future], timeout=secondary_timeout_s)
        latency_secondary_ms = (time.perf_counter() - secondary_start) * 1000.0

        outcome: DualReadOutcome
        secondary_result = None
        unreachable_reason: str | None = None

        if secondary_future in not_done:
            # Timed out — classify as aphelion_unreachable.
            secondary_future.cancel()
            outcome = "aphelion_unreachable"
            unreachable_reason = "timeout"
        else:
            # Future completed — check for exception.
            exc = secondary_future.exception()
            if exc is None:
                secondary_result = secondary_future.result()
                # Compare hits.
                if _hits_equal(primary_result, secondary_result):  # type: ignore[arg-type]
                    outcome = "match"
                else:
                    outcome = "diverge"
            elif isinstance(exc, AphelionUnreachableError):
                outcome = "aphelion_unreachable"
                unreachable_reason = exc.reason
            else:
                # Unexpected exception — logic bug, not infra unavailability.
                # Log only class name + str, NO stack trace (avoids PII in logs).
                _log.warning(
                    "secondary_unexpected_exception",
                    extra={
                        "event": "secondary_unexpected_exception",
                        "exc_class": type(exc).__name__,
                        "exc_str": str(exc),
                    },
                )
                outcome = "primary_only"
                secondary_result = None

        result = DualReadResult(
            outcome=outcome,
            primary=primary_result,  # type: ignore[arg-type]
            secondary=secondary_result,  # type: ignore[arg-type]
            correlation_id=cid,
            latency_primary_ms=latency_primary_ms,
            latency_secondary_ms=latency_secondary_ms,
            aphelion_unreachable_reason=unreachable_reason,
        )

        self._record(request.user_id, outcome)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record(self, user_id: str, outcome: DualReadOutcome) -> None:
        """Record outcome to live counter + Prometheus gauges (optional)."""
        record_dual_read_outcome(user_id=user_id, outcome=outcome)
        if self._live_counter is not None:
            self._live_counter.record(user_id=user_id, outcome=outcome)
