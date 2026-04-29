"""M3-T0: Crosswalk bounded backfill + lazy populate (US-011).

Backfills the existing ``crosswalk`` table from the ``memories`` and
``claims`` corpus. Schema is already in place via m0011 + m0012 — this
module performs no DDL.

Lazy materialization helper for the future DualReadRouter: given a
content_hash, resolves the canonical_ref from the crosswalk table.
On miss, caller should invoke record_orphan_miss().

Aphelion is NOT called here. The aphelion_doc_id column stays NULL
during M3a; M4 will fill it.

CONCURRENCY CONTRACT (critical — see ``parallax/router/sqlite_gate.py``):
    ``backfill_crosswalk`` runs a multi-row scan + INSERT loop that can
    examine up to ``CROSSWALK_BACKFILL_BATCH_LIMIT`` rows (default 10000).
    It MUST NOT share its ``sqlite3.Connection`` with active dual-read
    traffic.  ``sqlite3.Connection`` is not thread-safe; running the
    backfill scan concurrently with a ``SQLiteGate``-mediated dual-read
    read on the same connection violates the cross-thread cursor
    invariant and risks ``sqlite3.ProgrammingError`` or — worse — a
    SIGSEGV in the C extension (the exact bug ``SQLiteGate`` was
    introduced to prevent).

    To enforce this, ``backfill_crosswalk`` calls
    ``SQLiteGate.is_connection_gated(conn)`` at entry and raises
    ``ValueError`` if the connection is currently wrapped by a live
    ``SQLiteGate``.  Operators must run this routine on a dedicated
    connection (typically a fresh ``parallax.sqlite_store.connect``).
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import sqlite3
from typing import Final

from parallax.router.sqlite_gate import SQLiteGate

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

    Raises:
        ValueError: if ``conn`` is currently wrapped by a live
            ``SQLiteGate``.  See module docstring for the rationale —
            the multi-row scan would race the gate's dual-read traffic
            on the same connection.
    """
    if SQLiteGate.is_connection_gated(conn):
        raise ValueError(
            "backfill_crosswalk(conn=...) called with a connection that is "
            "currently wrapped by a live SQLiteGate. Use a dedicated "
            "sqlite3.Connection for the backfill (see module docstring)."
        )
    limit = _get_batch_limit(batch_limit)
    now = datetime.datetime.now(datetime.UTC).isoformat()

    rows_examined = 0
    rows_inserted = 0
    rows_skipped = 0
    source_breakdown: dict[str, int] = {"memory": 0, "claim": 0}

    # --- Memories ---
    for row in conn.execute(
        "SELECT memory_id, content_hash, source_id FROM memories WHERE user_id = ?"
        " ORDER BY created_at ASC, memory_id ASC",
        (user_id,),
    ):
        if rows_examined >= limit:
            return BackfillStats(
                rows_examined=rows_examined,
                rows_inserted=rows_inserted,
                rows_skipped_existing=rows_skipped,
                batch_limit_reached=True,
                source_breakdown=source_breakdown,
            )
        canonical_ref = f"memory:{row[0]}"
        insert_cur = conn.execute(
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
        if insert_cur.rowcount > 0:
            rows_inserted += 1
            source_breakdown["memory"] += 1
        else:
            rows_skipped += 1

    # --- Claims ---
    for row in conn.execute(
        "SELECT claim_id, content_hash, source_id FROM claims WHERE user_id = ?"
        " ORDER BY created_at ASC, claim_id ASC",
        (user_id,),
    ):
        if rows_examined >= limit:
            return BackfillStats(
                rows_examined=rows_examined,
                rows_inserted=rows_inserted,
                rows_skipped_existing=rows_skipped,
                batch_limit_reached=True,
                source_breakdown=source_breakdown,
            )
        canonical_ref = f"claim:{row[0]}"
        insert_cur = conn.execute(
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
        if insert_cur.rowcount > 0:
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
