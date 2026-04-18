"""Migration 0006 — events.session_id + session indexes.

Adds the session-continuity dimension to the events log. v0.3.0 starts
writing one event row per Claude Code hook fire (SessionStart / Stop /
PreToolUse / PostToolUse), and every row needs a ``session_id`` so the
retrieval API can slice events by session.

* up() is an additive ALTER TABLE plus two new indexes — cheap on both
  fresh and populated DBs.
* down() is a full SQLite table-swap that rebuilds ``events`` without
  ``session_id`` and re-attaches the append-only triggers from m0002
  plus the ``idx_events_user_time`` covering index from m0004. We do NOT
  rely on ALTER TABLE DROP COLUMN because the project supports SQLite
  versions older than 3.35.
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    "ALTER TABLE events ADD COLUMN session_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_type_session ON events(event_type, session_id)",
]


# down() uses the SQLite table-swap pattern to portably drop the
# session_id column. Pre-existing indexes (idx_events_target,
# idx_events_type_time, idx_events_user_time) and the append-only
# triggers (events_no_update, events_no_delete) must be recreated
# because DROP TABLE tears them down together with the old table.
DOWN_STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS idx_events_type_session",
    "DROP INDEX IF EXISTS idx_events_session",
    "DROP INDEX IF EXISTS idx_events_user_time",
    "DROP INDEX IF EXISTS idx_events_type_time",
    "DROP INDEX IF EXISTS idx_events_target",
    "DROP TRIGGER IF EXISTS events_no_update",
    "DROP TRIGGER IF EXISTS events_no_delete",
    """
    CREATE TABLE events_old (
        event_id       TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        actor          TEXT NOT NULL,
        event_type     TEXT NOT NULL,
        target_kind    TEXT,
        target_id      TEXT,
        payload_json   TEXT NOT NULL,
        approval_tier  TEXT,
        created_at     TIMESTAMP NOT NULL
    )
    """,
    """
    INSERT INTO events_old (
        event_id, user_id, actor, event_type, target_kind, target_id,
        payload_json, approval_tier, created_at
    )
    SELECT event_id, user_id, actor, event_type, target_kind, target_id,
           payload_json, approval_tier, created_at
    FROM events
    """,
    "DROP TABLE events",
    "ALTER TABLE events_old RENAME TO events",
    "CREATE INDEX IF NOT EXISTS idx_events_target    ON events(target_kind, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_user_time ON events(user_id, created_at)",
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


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    for stmt in DOWN_STATEMENTS:
        conn.execute(stmt)
