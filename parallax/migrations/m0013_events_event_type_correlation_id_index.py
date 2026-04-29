"""Migration 0013 — composite index on ``events(event_type, target_id)``.

M3b Phase 2 (US-005-T2.2) — the conflict-event writer dedups by querying
``events`` for rows whose ``event_type='arbitration_conflict'`` and whose
``target_id`` carries the ``canonical_ref`` (the writer stores the dedup
key directly in ``target_id`` so the SELECT does not have to scan
``payload_json`` for every candidate row). The 1-hour window check piggy-backs
on ``created_at`` from the same row.

The events table is free-form TEXT for ``event_type`` — no CHECK
constraint blocks ``arbitration_conflict``. The pre-existing indexes
(``idx_events_target`` on ``(target_kind, target_id)`` and
``idx_events_type_time`` on ``(event_type, created_at)`` from m0001) do
NOT cover the conflict-writer dedup query well: ``idx_events_target``
prefixes on ``target_kind`` (NULL for our envelope-style rows), and
``idx_events_type_time`` orders by time which adds churn for the dedup
SELECT that filters by both columns equally.

This migration adds a composite index keyed on ``(event_type,
target_id)`` so the dedup SELECT stays O(log n).
``CREATE INDEX IF NOT EXISTS`` keeps it idempotent.

Reversibility: ``down`` drops the index — no row data changes, so the
rollback is a single ``DROP INDEX IF EXISTS``.

down = DROP INDEX idx_events_event_type_target_id
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_events_event_type_target_id "
    "ON events(event_type, target_id)",
]

DOWN_STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS idx_events_event_type_target_id",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    for stmt in DOWN_STATEMENTS:
        conn.execute(stmt)
