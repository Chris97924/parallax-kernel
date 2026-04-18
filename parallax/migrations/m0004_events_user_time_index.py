"""Migration 0004 — covering index on events(user_id, created_at).

Supports the watermark scan in :func:`parallax.index._last_event_id` and
future per-user replay queries. Without this index those queries degrade
to a full table scan as the events log grows.
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_events_user_time ON events(user_id, created_at)",
]

DOWN_STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS idx_events_user_time",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    for stmt in DOWN_STATEMENTS:
        conn.execute(stmt)
