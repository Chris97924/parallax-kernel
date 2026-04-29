"""Write ``arbitration_conflict`` rows to the events table (M3b — US-005).

When :class:`~parallax.router.live_arbitration.LiveArbitrationDecision.requires_manual_review`
is ``True`` (winning_source in ``{"tie", "fallback"}``), the DualReadRouter
emits a single envelope row per (canonical_ref, conflict_field) pair so an
operator can later replay the conflict offline.

Pure module — no I/O at import. The writer is best-effort: any failure in
the dedup SELECT or the INSERT is swallowed and surfaced as a returned
empty event_id, honoring the fail-closed invariant from US-001-HIGH1.

Public surface
--------------
- :func:`write_conflict_event` — append one envelope row, dedup'd within
  a 1-hour window keyed on ``(canonical_ref, conflict_field)``.
- :data:`NO_CANONICAL_REF_SENTINEL` — stable string used when neither
  primary nor secondary returned hits.
- :class:`WriteFailure` — sentinel exception class exported for callers
  that want to introspect failures from telemetry hooks.  Never raised
  out of :func:`write_conflict_event`.
- :func:`get_dedup_hit_count` — process-local counter (KISS: single
  process, no cross-process consistency needed for telemetry).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Mapping
from typing import Any

from parallax.obs.log import get_logger
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.live_arbitration import LiveArbitrationDecision
from parallax.router.types import DataQualityFlag

__all__ = [
    "NO_CANONICAL_REF_SENTINEL",
    "SCHEMA_VERSION",
    "DEDUP_WINDOW_SECONDS",
    "WriteFailure",
    "write_conflict_event",
    "get_dedup_hit_count",
]

_log = get_logger("parallax.events.conflict_writer")

NO_CANONICAL_REF_SENTINEL = "__no_canonical_ref__"
SCHEMA_VERSION = "1.0"
DEDUP_WINDOW_SECONDS = 3600  # 1 hour


class WriteFailure(Exception):
    """Sentinel — never raised out of :func:`write_conflict_event`.

    Callers that hook into observability may import this class to type
    custom telemetry handlers; the writer itself swallows every exception
    and returns the empty string instead.
    """


# ---------------------------------------------------------------------------
# Process-local counters (KISS: telemetry only, single-process consistency)
# ---------------------------------------------------------------------------

_dedup_hits: dict[str, int] = {"count": 0}


def get_dedup_hit_count() -> int:
    """Return the number of dedup hits observed so far in this process."""
    return _dedup_hits["count"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence_first_id(ev: RetrievalEvidence | None) -> str | None:
    if ev is None or len(ev.hits) == 0:
        return None
    first = ev.hits[0]
    if isinstance(first, Mapping):
        ref = first.get("id")
        if isinstance(ref, str) and ref:
            return ref
    return None


def _derive_canonical_ref(payload: Mapping[str, Any]) -> str:
    """Pick the dedup key from the payload's primary / secondary evidence.

    Order:
      1. Primary's first hit ``id`` if non-empty.
      2. Secondary's first hit ``id`` if non-empty.
      3. :data:`NO_CANONICAL_REF_SENTINEL` (stable, documented).
    """
    primary = payload.get("primary") if isinstance(payload, Mapping) else None
    secondary = payload.get("secondary") if isinstance(payload, Mapping) else None
    if isinstance(primary, RetrievalEvidence):
        ref = _evidence_first_id(primary)
        if ref is not None:
            return ref
    if isinstance(secondary, RetrievalEvidence):
        ref = _evidence_first_id(secondary)
        if ref is not None:
            return ref
    return NO_CANONICAL_REF_SENTINEL


def _decision_payload_dict(decision: LiveArbitrationDecision) -> dict:
    """Round-trip a frozen decision into a plain dict via its JSON line.

    Round-tripping (rather than reading dataclass fields directly) keeps
    the serialized envelope identical to whatever
    ``decision.to_json_line`` would emit on its own — one source of truth.
    """
    return json.loads(decision.to_json_line())


def _build_envelope(
    decision: LiveArbitrationDecision,
    *,
    timestamp_us_utc: int,
    data_quality_flag: DataQualityFlag,
) -> dict:
    return {
        "event_type": "arbitration_conflict",
        "correlation_id": decision.correlation_id,
        "timestamp_us_utc": timestamp_us_utc,
        "schema_version": SCHEMA_VERSION,
        "payload": _decision_payload_dict(decision),
        "data_quality_flag": data_quality_flag.value,
    }


def _now_iso_from_us(now_us_utc: int) -> str:
    """Return an ISO-8601 UTC string for ``now_us_utc`` (microseconds)."""
    import datetime as _dt

    seconds, micros = divmod(now_us_utc, 1_000_000)
    return (
        _dt.datetime.fromtimestamp(seconds, _dt.UTC)
        .replace(microsecond=micros)
        .isoformat(timespec="microseconds")
    )


def _select_existing_event_id(
    conn: sqlite3.Connection,
    *,
    canonical_ref: str,
    conflict_field: str,
    window_start_us: int,
) -> str | None:
    """Look up a non-expired conflict event for (canonical_ref, conflict_field).

    Stored layout (writer-side contract):
        target_kind = "arbitration_conflict_dedup"
        target_id   = canonical_ref
        approval_tier = conflict_field    -- piggybacked dedup column
        actor       = NULL-equivalent ("system")

    The dedup row is the same row as the envelope; we re-purpose
    ``approval_tier`` rather than mint a new column so the migration stays
    index-only.
    """
    rows = conn.execute(
        "SELECT event_id, payload_json FROM events "
        "WHERE event_type = ? "
        "AND target_id = ? "
        "AND approval_tier = ? "
        "ORDER BY created_at DESC",
        ("arbitration_conflict", canonical_ref, conflict_field),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        ts = payload.get("timestamp_us_utc")
        if not isinstance(ts, int):
            continue
        if ts >= window_start_us:
            return str(row["event_id"])
    return None


def _insert_event_row(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    user_id: str,
    actor: str,
    canonical_ref: str,
    conflict_field: str,
    payload_json: str,
    created_at: str,
) -> None:
    """Append a single ``arbitration_conflict`` envelope row.

    Carved into a helper so tests can monkey-patch the insert without
    poking the (real) sqlite connection used elsewhere in the test.
    """
    conn.execute(
        "INSERT INTO events "
        "(event_id, user_id, actor, event_type, target_kind, target_id, "
        " payload_json, approval_tier, created_at, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            user_id,
            actor,
            "arbitration_conflict",
            "arbitration_conflict_dedup",
            canonical_ref,
            payload_json,
            conflict_field,
            created_at,
            None,
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_conflict_event(
    decision: LiveArbitrationDecision,
    payload: Mapping[str, Any],
    conn: sqlite3.Connection,
    *,
    data_quality_flag: DataQualityFlag = DataQualityFlag.COLD_START,
    now_us_utc: int | None = None,
) -> str:
    """Append a single ``arbitration_conflict`` envelope row.

    Idempotent within a 1-hour window keyed on ``(canonical_ref,
    conflict_field)``: the second call returns the existing event_id and
    does NOT insert a new row.

    Best-effort: ANY exception (dedup SELECT, INSERT, payload encoding)
    is swallowed and surfaced as a returned empty string. The
    DualReadRouter requires this so observability code can never break
    the canonical query path (US-001-HIGH1 fail-closed invariant).

    Parameters
    ----------
    decision:
        The arbitration verdict.  ``correlation_id`` and
        ``tie_breaker_rule`` come from here.
    payload:
        Mapping containing ``"primary"`` and/or ``"secondary"``
        :class:`RetrievalEvidence` plus optional ``"user_id"``.  Used to
        derive ``canonical_ref``.
    conn:
        SQLite connection. The caller owns transactional boundaries; the
        writer issues a single INSERT and lets autocommit / surrounding
        ``with conn:`` block handle commit semantics.
    data_quality_flag:
        Defaults to :data:`DataQualityFlag.COLD_START`. Story 6 will wire
        a stricter value via the rollout CLI.
    now_us_utc:
        Optional override for the envelope timestamp + dedup-window
        clock. Tests use this to simulate clock advances; production
        callers leave it unset.

    Returns
    -------
    str
        The event_id of the row (newly inserted or existing dedup hit).
        Empty string when the write failed.
    """
    try:
        ts_us = now_us_utc if now_us_utc is not None else time.time_ns() // 1_000
        canonical_ref = _derive_canonical_ref(payload)
        conflict_field = decision.tie_breaker_rule

        # -- Dedup window check -----------------------------------------
        window_start_us = ts_us - DEDUP_WINDOW_SECONDS * 1_000_000
        existing = _select_existing_event_id(
            conn,
            canonical_ref=canonical_ref,
            conflict_field=conflict_field,
            window_start_us=window_start_us,
        )
        if existing is not None:
            _dedup_hits["count"] += 1
            return existing

        # -- Build envelope ---------------------------------------------
        envelope = _build_envelope(
            decision,
            timestamp_us_utc=ts_us,
            data_quality_flag=data_quality_flag,
        )
        payload_json = json.dumps(envelope, sort_keys=True)
        event_id = uuid.uuid4().hex
        user_id = ""
        if isinstance(payload, Mapping):
            uid = payload.get("user_id")
            if isinstance(uid, str):
                user_id = uid

        _insert_event_row(
            conn,
            event_id=event_id,
            user_id=user_id,
            actor="system",
            canonical_ref=canonical_ref,
            conflict_field=conflict_field,
            payload_json=payload_json,
            created_at=_now_iso_from_us(ts_us),
        )
        return event_id
    except Exception as exc:  # noqa: BLE001 — fail-closed: never crash the request
        try:
            _log.warning(
                "conflict_writer_failed",
                extra={
                    "event": "conflict_writer_failed",
                    "exc_class": type(exc).__name__,
                    "exc_str": str(exc),
                    "correlation_id": getattr(decision, "correlation_id", ""),
                },
            )
        except Exception:  # noqa: BLE001 — last-resort: even logger may be broken
            pass
        return ""
