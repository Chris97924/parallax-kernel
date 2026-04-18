"""High-level ingest helpers for Parallax.

Wraps :mod:`parallax.sqlite_store` with UPSERT semantics:

* Every direct-input call (``source_id=None``) lazily creates a synthetic
  source row keyed ``direct:<user_id>``. This is what closes the UNIQUE
  NULL-hole on ``claims.source_id``.
* content_hash collisions are absorbed -- the existing row's id is returned
  and no duplicate is written. Re-ingestion is therefore safe and the
  "reaffirm" branch is silent in Phase-0 (see :func:`parallax.sqlite_store.reaffirm`).
"""

from __future__ import annotations

import sqlite3
import time

from ulid import ULID

from parallax import telemetry
from parallax.events import record_memory_reaffirmed
from parallax.hashing import content_hash
from parallax.obs.log import get_logger
from parallax.obs.metrics import get_counter
from parallax.sqlite_store import (
    Claim,
    Memory,
    Source,
    insert_claim,
    insert_memory,
    insert_source,
    now_iso,
    query,
)
from parallax.transitions import CLAIM_TRANSITIONS

_log = get_logger("parallax.ingest")
_tlog = telemetry.get_logger("parallax.ingest.telemetry")
_c_memory = get_counter("ingest_memory_total")
_c_claim = get_counter("ingest_claim_total")
_c_dedup = get_counter("dedup_hit_total")

__all__ = [
    "ingest_memory",
    "ingest_claim",
    "synthetic_direct_source_id",
]


def _ulid() -> str:
    return str(ULID())


def synthetic_direct_source_id(user_id: str) -> str:
    """Return the canonical synthetic source id for direct input."""
    return f"direct:{user_id}"


def _ensure_direct_source(conn: sqlite3.Connection, user_id: str) -> str:
    source_id = synthetic_direct_source_id(user_id)
    insert_source(
        conn,
        Source(
            source_id=source_id,
            uri=f"parallax://direct/{user_id}",
            kind="chat",
            content_hash=content_hash(source_id),
            user_id=user_id,
            ingested_at=now_iso(),
            state="ingested",
        ),
    )
    return source_id


def ingest_memory(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    title: str | None,
    summary: str | None,
    vault_path: str,
    source_id: str | None = None,
) -> str:
    """UPSERT a memory row. Returns the persisted memory_id.

    Race-safe: attempts INSERT OR IGNORE then re-selects by the UNIQUE
    (content_hash, user_id) index so the caller always receives the id of
    the row that is actually persisted, never a ULID that was silently
    dropped by the IGNORE branch.
    """
    start = time.perf_counter()
    try:
        if source_id is None:
            source_id = _ensure_direct_source(conn, user_id)

        # hashing.normalize rejects None explicitly (v0.1.2 boundary
        # contract); Memory.title/summary remain Optional[str] at the
        # storage layer, so we canonicalize None -> "" here before hashing.
        title_for_hash = "" if title is None else title
        summary_for_hash = "" if summary is None else summary
        ch = content_hash(title_for_hash, summary_for_hash, vault_path)
        now = now_iso()
        new_id = _ulid()
        insert_memory(
            conn,
            Memory(
                memory_id=new_id,
                user_id=user_id,
                source_id=source_id,
                vault_path=vault_path,
                title=title,
                summary=summary,
                content_hash=ch,
                state="active",
                created_at=now,
                updated_at=now,
            ),
        )
        row = query(
            conn,
            "SELECT memory_id FROM memories WHERE content_hash = ? AND user_id = ?",
            (ch, user_id),
        )
        persisted_id = row[0]["memory_id"]
        _c_memory.inc()
        telemetry.inc("ingested_total")
        deduped = persisted_id != new_id
        if deduped:
            _c_dedup.inc()
            telemetry.inc("dedup_hits_total")
            telemetry.emit_dedup_hit(_tlog, kind="memory", user_id=user_id, memory_id=persisted_id)
            record_memory_reaffirmed(conn, user_id=user_id, memory_id=persisted_id)
        _log.info(
            "ingest_memory",
            extra={"event": "ingest_memory", "user_id": user_id, "memory_id": persisted_id,
                   "deduped": deduped},
        )
        return persisted_id
    except Exception as exc:
        telemetry.emit_ingest_error(_tlog, kind="memory", user_id=user_id, error=str(exc))
        raise
    finally:
        telemetry.observe_latency_ms((time.perf_counter() - start) * 1000.0)


def ingest_claim(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    subject: str,
    predicate: str,
    object_: str,
    source_id: str | None = None,
    confidence: float | None = None,
    state: str = "auto",
) -> str:
    """UPSERT a claim row. Returns the persisted claim_id.

    Race-safe via INSERT OR IGNORE + re-select on the UNIQUE
    (content_hash, source_id) index. See :func:`ingest_memory` for rationale.

    ``state`` must be a registered initial state in
    :data:`parallax.transitions.CLAIM_TRANSITIONS` (defensive validation at
    the ingest boundary per the input-validation rule); defaults to
    ``'auto'`` to preserve prior behaviour. The extract layer uses
    ``state='pending'`` for low-confidence claims that need review.
    """
    if state not in CLAIM_TRANSITIONS:
        raise ValueError(
            f"invalid claim state {state!r}; "
            f"expected one of {sorted(CLAIM_TRANSITIONS)}"
        )
    start = time.perf_counter()
    try:
        if source_id is None:
            source_id = _ensure_direct_source(conn, user_id)

        ch = content_hash(subject, predicate, object_, source_id)
        now = now_iso()
        new_id = _ulid()
        insert_claim(
            conn,
            Claim(
                claim_id=new_id,
                user_id=user_id,
                subject=subject,
                predicate=predicate,
                object=object_,
                source_id=source_id,
                content_hash=ch,
                confidence=confidence,
                state=state,
                created_at=now,
                updated_at=now,
            ),
        )
        row = query(
            conn,
            "SELECT claim_id FROM claims WHERE content_hash = ? AND source_id = ?",
            (ch, source_id),
        )
        persisted_id = row[0]["claim_id"]
        _c_claim.inc()
        telemetry.inc("ingested_total")
        deduped = persisted_id != new_id
        if deduped:
            _c_dedup.inc()
            telemetry.inc("dedup_hits_total")
            telemetry.emit_dedup_hit(_tlog, kind="claim", user_id=user_id, claim_id=persisted_id)
        _log.info(
            "ingest_claim",
            extra={"event": "ingest_claim", "user_id": user_id, "claim_id": persisted_id,
                   "deduped": deduped},
        )
        return persisted_id
    except Exception as exc:
        telemetry.emit_ingest_error(_tlog, kind="claim", user_id=user_id, error=str(exc))
        raise
    finally:
        telemetry.observe_latency_ms((time.perf_counter() - start) * 1000.0)
