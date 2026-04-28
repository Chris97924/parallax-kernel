"""M3-T0: Crosswalk bounded backfill + lazy populate (US-011).

Backfills the existing ``crosswalk`` table from the ``memories`` and
``claims`` corpus. Schema is already in place via m0011 + m0012 — this
module performs no DDL.

Lazy materialization helper for the future DualReadRouter: given a
content_hash, resolves the canonical_ref from the crosswalk table.
On miss, caller should invoke record_orphan_miss().

Aphelion is NOT called here. The aphelion_doc_id column stays NULL
during M3a; M4 will fill it.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import sqlite3
from typing import Final

try:
    from prometheus_client import Counter as _PromCounter

    _ORPHAN_COUNTER = _PromCounter(
        "parallax_crosswalk_miss_orphan_total",
        "Number of crosswalk lookup misses where content was never ingested.",
        ["user_id"],
    )
except ValueError:
    # Already registered in the default registry (e.g. test re-imports).
    import prometheus_client

    _ORPHAN_COUNTER = prometheus_client.REGISTRY._names_to_collectors[  # type: ignore[attr-defined]
        "parallax_crosswalk_miss_orphan_total"
    ]

__all__ = [
    "BACKFILL_BATCH_LIMIT_DEFAULT",
    "BackfillStats",
    "backfill_crosswalk",
    "lazy_materialize_by_content_hash",
    "record_orphan_miss",
]

# Default is read at function-call time from env (same dynamic pattern as
# parallax/router/config.py:is_router_enabled). NOT captured at module load.
BACKFILL_BATCH_LIMIT_DEFAULT: Final[int] = 10_000


@dataclasses.dataclass(frozen=True)
class BackfillStats:
    rows_examined: int
    rows_inserted: int
    rows_skipped_existing: int
    batch_limit_reached: bool
    source_breakdown: dict[str, int]


def _get_batch_limit(batch_limit: int | None) -> int:
    """Return the effective batch limit, reading env at call time if needed."""
    if batch_limit is not None:
        return batch_limit
    raw = os.getenv("CROSSWALK_BACKFILL_BATCH_LIMIT", "")
    if raw.strip().isdigit():
        return int(raw.strip())
    return BACKFILL_BATCH_LIMIT_DEFAULT


def backfill_crosswalk(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    batch_limit: int | None = None,
) -> BackfillStats:
    """Populate the crosswalk table from the memories + claims corpus.

    Uses INSERT OR IGNORE so re-runs are idempotent. Processes at most
    ``batch_limit`` rows total across both sources. If ``batch_limit`` is
    None, reads the env var ``CROSSWALK_BACKFILL_BATCH_LIMIT`` at call
    time (default 10000).

    Returns BackfillStats describing what was examined and inserted.
    aphelion_doc_id / vault_path / last_event_id_seen / last_embedded_at
    are all left NULL — Aphelion wiring is M4 work.
    """
    limit = _get_batch_limit(batch_limit)
    now = datetime.datetime.now(datetime.UTC).isoformat()

    rows_examined = 0
    rows_inserted = 0
    rows_skipped = 0
    source_breakdown: dict[str, int] = {"memory": 0, "claim": 0}

    # --- Memories ---
    memory_rows = conn.execute(
        "SELECT memory_id, content_hash, source_id FROM memories WHERE user_id = ?"
        " ORDER BY created_at ASC, memory_id ASC",
        (user_id,),
    ).fetchall()

    for row in memory_rows:
        if rows_examined >= limit:
            return BackfillStats(
                rows_examined=rows_examined,
                rows_inserted=rows_inserted,
                rows_skipped_existing=rows_skipped,
                batch_limit_reached=True,
                source_breakdown=source_breakdown,
            )
        canonical_ref = f"memory:{row[0]}"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO crosswalk (
                user_id, canonical_ref, parallax_target_kind, parallax_target_id,
                query_type, state, content_hash, source_id,
                vault_path, aphelion_doc_id, last_event_id_seen, last_embedded_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                canonical_ref,
                "memory",
                row[0],
                None,  # query_type — set later by live router
                "mapped",
                row[1],  # content_hash
                row[2],  # source_id (may be NULL)
                None,  # vault_path — M4
                None,  # aphelion_doc_id — M4
                None,  # last_event_id_seen
                None,  # last_embedded_at
                now,
                now,
            ),
        )
        rows_examined += 1
        if cursor.rowcount > 0:
            rows_inserted += 1
            source_breakdown["memory"] += 1
        else:
            rows_skipped += 1

    # --- Claims ---
    claim_rows = conn.execute(
        "SELECT claim_id, content_hash, source_id FROM claims WHERE user_id = ?"
        " ORDER BY created_at ASC, claim_id ASC",
        (user_id,),
    ).fetchall()

    for row in claim_rows:
        if rows_examined >= limit:
            return BackfillStats(
                rows_examined=rows_examined,
                rows_inserted=rows_inserted,
                rows_skipped_existing=rows_skipped,
                batch_limit_reached=True,
                source_breakdown=source_breakdown,
            )
        canonical_ref = f"claim:{row[0]}"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO crosswalk (
                user_id, canonical_ref, parallax_target_kind, parallax_target_id,
                query_type, state, content_hash, source_id,
                vault_path, aphelion_doc_id, last_event_id_seen, last_embedded_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                canonical_ref,
                "claim",
                row[0],
                None,  # query_type
                "mapped",
                row[1],  # content_hash
                row[2],  # source_id
                None,  # vault_path
                None,  # aphelion_doc_id — M4
                None,  # last_event_id_seen
                None,  # last_embedded_at
                now,
                now,
            ),
        )
        rows_examined += 1
        if cursor.rowcount > 0:
            rows_inserted += 1
            source_breakdown["claim"] += 1
        else:
            rows_skipped += 1

    return BackfillStats(
        rows_examined=rows_examined,
        rows_inserted=rows_inserted,
        rows_skipped_existing=rows_skipped,
        batch_limit_reached=False,
        source_breakdown=source_breakdown,
    )


def lazy_materialize_by_content_hash(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    content_hash: str,
) -> str | None:
    """Look up an existing crosswalk row by (user_id, content_hash).

    Returns the canonical_ref string if found, None on miss.
    Pure read — no INSERT, no Aphelion call.
    On None return the caller is responsible for incrementing the orphan
    counter via record_orphan_miss().
    """
    row = conn.execute(
        "SELECT canonical_ref FROM crosswalk WHERE user_id = ? AND content_hash = ?",
        (user_id, content_hash),
    ).fetchone()
    if row is None:
        return None
    return row[0]


def record_orphan_miss(*, user_id: str) -> None:
    """Increment parallax_crosswalk_miss_orphan_total for user_id by 1."""
    _ORPHAN_COUNTER.labels(user_id=user_id).inc()
