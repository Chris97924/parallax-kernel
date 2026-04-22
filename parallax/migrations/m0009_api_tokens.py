"""Migration 0009 — per-user API token table (multi-user mode).

Adds the ``api_tokens`` table + ``idx_api_tokens_user_id`` supporting
index. Rows bind a sha256 token hash to a ``user_id`` so the HTTP layer
can resolve ``Authorization: Bearer <token>`` into the owning principal
when ``PARALLAX_MULTI_USER=1`` is set.

Only the token HASH is persisted — the plaintext is shown once on
``parallax token create`` and never again. Revocation is soft: rows set
``revoked_at`` to the current ISO-8601 timestamp so the audit trail is
preserved for post-mortems.

Schema:
    api_tokens(
        token_hash  TEXT PRIMARY KEY,   -- sha256 hex
        user_id     TEXT NOT NULL,      -- principal this token authenticates
        created_at  TEXT NOT NULL,      -- now_iso() at creation
        revoked_at  TEXT,               -- now_iso() when revoked, else NULL
        label       TEXT                -- optional operator-visible tag
    )
    idx_api_tokens_user_id ON api_tokens(user_id)
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    "CREATE TABLE IF NOT EXISTS api_tokens ("
    "token_hash TEXT PRIMARY KEY, "
    "user_id TEXT NOT NULL, "
    "created_at TEXT NOT NULL, "
    "revoked_at TEXT, "
    "label TEXT"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_api_tokens_user_id ON api_tokens(user_id)",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_api_tokens_user_id")
    conn.execute("DROP TABLE IF EXISTS api_tokens")
