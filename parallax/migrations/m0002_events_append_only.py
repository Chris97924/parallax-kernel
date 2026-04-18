"""Migration 0002 — events append-only triggers.

Promotes the events append-only contract from app-layer convention
(``sqlite_store.__all__`` whitelist) to a DB-level guarantee. Any UPDATE or
DELETE on ``events`` raises ``sqlite3.IntegrityError`` regardless of which
client opened the connection.
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    """
    CREATE TRIGGER IF NOT EXISTS events_no_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_no_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events are append-only');
    END
    """,
]

DOWN_STATEMENTS: list[str] = [
    "DROP TRIGGER IF EXISTS events_no_delete",
    "DROP TRIGGER IF EXISTS events_no_update",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    for stmt in DOWN_STATEMENTS:
        conn.execute(stmt)
