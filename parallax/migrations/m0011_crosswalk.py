"""Migration 0011 — crosswalk table for dual-memory router mapping.

Adds ``crosswalk`` as the canonical mapping table between Parallax entities
and external semantic documents (e.g., Aphelion/Engram adapters).
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    "CREATE TABLE IF NOT EXISTS crosswalk ("
    "user_id TEXT NOT NULL, "
    "canonical_ref TEXT NOT NULL, "
    "parallax_target_kind TEXT NOT NULL, "
    "parallax_target_id TEXT NOT NULL, "
    "query_type TEXT, "
    "state TEXT NOT NULL CHECK(state IN ('mapped','unmapped','conflict')), "
    "content_hash TEXT NOT NULL, "
    "source_id TEXT, "
    "vault_path TEXT, "
    "dpkg_doc_id TEXT, "
    "last_event_id_seen TEXT, "
    "last_embedded_at TIMESTAMP, "
    "created_at TIMESTAMP NOT NULL, "
    "updated_at TIMESTAMP NOT NULL, "
    "PRIMARY KEY(user_id, canonical_ref)"
    ")",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_crosswalk_user_target "
    "ON crosswalk(user_id, parallax_target_kind, parallax_target_id)",
    "CREATE INDEX IF NOT EXISTS idx_crosswalk_user_state "
    "ON crosswalk(user_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_crosswalk_user_query_type "
    "ON crosswalk(user_id, query_type)",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_crosswalk_user_query_type")
    conn.execute("DROP INDEX IF EXISTS idx_crosswalk_user_state")
    conn.execute("DROP INDEX IF EXISTS idx_crosswalk_user_target")
    conn.execute("DROP TABLE IF EXISTS crosswalk")

