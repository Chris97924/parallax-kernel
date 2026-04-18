"""Migration 0001 — initial Parallax schema.

Idempotent CREATE IF NOT EXISTS so an already-bootstrapped DB can have this
migration recorded as applied without duplicate-table errors. Mirrors the
historical contents of ``schema.sql`` (the file remains as a human-readable
SSoT but is no longer the apply path).

PRAGMAs ``foreign_keys`` and ``journal_mode`` are NOT issued here — they are
per-connection settings and are established by :func:`parallax.sqlite_store.connect`
for every connection that will run migrations or app writes.
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS sources (
        source_id    TEXT PRIMARY KEY,
        uri          TEXT NOT NULL,
        kind         TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        user_id      TEXT NOT NULL,
        ingested_at  TIMESTAMP NOT NULL,
        state        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sources_user ON sources(user_id)",
    """
    CREATE TABLE IF NOT EXISTS memories (
        memory_id    TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        source_id    TEXT REFERENCES sources(source_id),
        vault_path   TEXT NOT NULL,
        title        TEXT,
        summary      TEXT,
        content_hash TEXT NOT NULL,
        state        TEXT NOT NULL,
        created_at   TIMESTAMP NOT NULL,
        updated_at   TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_memories_user_state ON memories(user_id, state)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_memories_content ON memories(content_hash, user_id)",
    """
    CREATE TABLE IF NOT EXISTS claims (
        claim_id     TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        subject      TEXT NOT NULL,
        predicate    TEXT NOT NULL,
        object       TEXT NOT NULL,
        source_id    TEXT NOT NULL REFERENCES sources(source_id),
        content_hash TEXT NOT NULL,
        confidence   REAL,
        state        TEXT NOT NULL,
        created_at   TIMESTAMP NOT NULL,
        updated_at   TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_claims_user_state ON claims(user_id, state)",
    "CREATE INDEX IF NOT EXISTS idx_claims_subject ON claims(user_id, subject)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_content ON claims(content_hash, source_id)",
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id    TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        target_kind    TEXT NOT NULL CHECK (target_kind IN ('claim','memory','source')),
        target_id      TEXT NOT NULL,
        action         TEXT NOT NULL,
        actor          TEXT NOT NULL,
        approval_tier  TEXT,
        state          TEXT NOT NULL,
        rationale      TEXT,
        created_at     TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_target ON decisions(target_kind, target_id)",
    """
    CREATE TABLE IF NOT EXISTS events (
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
    "CREATE INDEX IF NOT EXISTS idx_events_target ON events(target_kind, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, created_at)",
    """
    CREATE TABLE IF NOT EXISTS index_state (
        index_name       TEXT NOT NULL,
        version          INTEGER NOT NULL,
        last_built_at    TIMESTAMP,
        source_watermark TEXT,
        doc_count        INTEGER,
        state            TEXT NOT NULL,
        error_text       TEXT,
        PRIMARY KEY (index_name, version)
    )
    """,
]

DOWN_STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS idx_events_type_time",
    "DROP INDEX IF EXISTS idx_events_target",
    "DROP INDEX IF EXISTS idx_decisions_target",
    "DROP INDEX IF EXISTS uniq_claims_content",
    "DROP INDEX IF EXISTS idx_claims_subject",
    "DROP INDEX IF EXISTS idx_claims_user_state",
    "DROP INDEX IF EXISTS uniq_memories_content",
    "DROP INDEX IF EXISTS idx_memories_user_state",
    "DROP INDEX IF EXISTS idx_sources_user",
    "DROP TABLE IF EXISTS index_state",
    "DROP TABLE IF EXISTS events",
    "DROP TABLE IF EXISTS decisions",
    "DROP TABLE IF EXISTS claims",
    "DROP TABLE IF EXISTS memories",
    "DROP TABLE IF EXISTS sources",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    for stmt in DOWN_STATEMENTS:
        conn.execute(stmt)
