"""Migration 0008 — normalize legacy timestamp strings to canonical form.

Rewrites every ISO-8601 timestamp column across the schema to the exact
32-character ``now_iso()`` canonical layout
``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``. Permanently closes the v0.5.0-pre3
``naive_iso_same_second_as_since`` known-limitation: after this migration
runs, no stored string is short enough for SQLite lex-compare to flip
``created_at >= since`` to FALSE at the second boundary.

Rationale:
    ``_iso_normalize`` always emits the 32-char form. If a stored value is
    shorter (naive 19-char, or 25-char tz-without-micro), lex-compare
    becomes position-sensitive at index 19 and rows are silently dropped.
    Normalizing corpus-wide keeps retrieve.py on a single compare path
    with no per-row length branching.

Columns rewritten (every TIMESTAMP column that exists at version 7):
    sources(ingested_at)
    memories(created_at, updated_at)
    claims(created_at, updated_at)
    claim_metadata(last_seen_at, superseded_at, created_at, updated_at)
    decisions(created_at)
    events(created_at)
    index_state(last_built_at)

``events`` carries ``events_no_update`` / ``events_no_delete`` triggers
from m0002 that would abort any UPDATE. We drop those triggers for the
duration of the sweep and recreate them before the transaction commits,
so the append-only contract is still in force against app writes.

Idempotency:
    Re-running ``up`` against an already-normalized corpus is a no-op —
    canonical strings parse and re-emit to themselves under
    ``datetime.fromisoformat`` + ``isoformat(timespec='microseconds')``.
    The registry prevents double-application via ``schema_migrations``
    regardless.

The pure-SQL DDL placeholders in ``STATEMENTS`` exist only so the
static ``migration_plan`` estimator can classify impact as "UPDATE
<table>" per row-bearing table. The real rewrite runs in Python below
(a single ``conn.execute`` per row inside the ambient transaction).
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

# ``(table, columns)``. NULL values are filtered per-row in ``up()`` so
# nullable columns (``claim_metadata.superseded_at``, ``index_state.last_built_at``)
# don't need to be flagged here.
_TS_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sources", ("ingested_at",)),
    ("memories", ("created_at", "updated_at")),
    ("claims", ("created_at", "updated_at")),
    ("claim_metadata", ("last_seen_at", "superseded_at", "created_at", "updated_at")),
    ("decisions", ("created_at",)),
    ("events", ("created_at",)),
    ("index_state", ("last_built_at",)),
)

# Primary key column used for the per-row UPDATE ``WHERE`` clause.
_PK: dict[str, str] = {
    "sources": "source_id",
    "memories": "memory_id",
    "claims": "claim_id",
    "claim_metadata": "claim_id",
    "decisions": "decision_id",
    "events": "event_id",
    "index_state": "rowid",  # composite PK (index_name, version); rowid is stable
}

STATEMENTS: list[str] = [
    "DROP TRIGGER IF EXISTS events_no_update",
    "DROP TRIGGER IF EXISTS events_no_delete",
    "UPDATE sources SET ingested_at = canonical(ingested_at)",
    "UPDATE memories SET created_at = canonical(created_at), "
    "updated_at = canonical(updated_at)",
    "UPDATE claims SET created_at = canonical(created_at), "
    "updated_at = canonical(updated_at)",
    "UPDATE claim_metadata SET last_seen_at = canonical(last_seen_at), "
    "superseded_at = canonical(superseded_at), "
    "created_at = canonical(created_at), updated_at = canonical(updated_at)",
    "UPDATE decisions SET created_at = canonical(created_at)",
    "UPDATE events SET created_at = canonical(created_at)",
    "UPDATE index_state SET last_built_at = canonical(last_built_at)",
    # Triggers recreated after the sweep; kept as literal DDL so migration_plan
    # attributes them to the ``events`` table impact bucket.
    "CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events "
    "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events "
    "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END",
]


def _canonicalize(value: str) -> str:
    """Return the 32-char canonical form of an ISO-8601 timestamp.

    Accepts any form ``datetime.fromisoformat`` tolerates plus a trailing
    ``Z``. Naive inputs are treated as UTC — the repo-wide convention is
    UTC-only storage, and the rows this migration is here to fix are by
    definition naive-UTC (written pre-``now_iso()``).
    """
    ts = value[:-1] + "+00:00" if value.endswith("Z") else value
    dt = _dt.datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    dt = dt.astimezone(_dt.UTC)
    return dt.isoformat(timespec="microseconds")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def up(conn: sqlite3.Connection) -> None:
    # Lift the append-only triggers so we can rewrite ``events.created_at``.
    # Both will be recreated below, inside the same ambient transaction.
    conn.execute("DROP TRIGGER IF EXISTS events_no_update")
    conn.execute("DROP TRIGGER IF EXISTS events_no_delete")

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for table, columns in _TS_COLUMNS:
            if not _table_exists(conn, table):
                continue
            pk = _PK[table]
            cols_sql = ", ".join(columns)
            rows = conn.execute(
                f"SELECT {pk} AS _pk, {cols_sql} FROM {table}"
            ).fetchall()
            for r in rows:
                updates: dict[str, str] = {}
                for col in columns:
                    raw = r[col]
                    if raw is None:
                        continue
                    new_val = _canonicalize(raw)
                    if new_val != raw:
                        updates[col] = new_val
                if not updates:
                    continue
                set_clause = ", ".join(f"{c} = ?" for c in updates)
                conn.execute(
                    f"UPDATE {table} SET {set_clause} WHERE {pk} = ?",
                    (*updates.values(), r["_pk"]),
                )
    finally:
        # Always restore row_factory and recreate the append-only triggers,
        # even if the sweep raised. The ambient ``_manual_tx`` in
        # ``migrate_to_latest`` will ROLLBACK DDL on exception — so the
        # triggers come back either way — but running ``up()`` bare (e.g.
        # from a test) must also leave events protected.
        conn.row_factory = prev_factory
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS events_no_update "
            "BEFORE UPDATE ON events "
            "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS events_no_delete "
            "BEFORE DELETE ON events "
            "BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
        )


def down(conn: sqlite3.Connection) -> None:
    # Corpus normalization is lossless in the forward direction — every
    # shape (naive, tz-no-micro, canonical) maps to the same canonical
    # output. There is no faithful inverse, and producing one would mean
    # fabricating shorter strings. down() is therefore a no-op, matching
    # the pattern used for data-content migrations that cannot be
    # mechanically reversed without corpus-specific heuristics.
    del conn
