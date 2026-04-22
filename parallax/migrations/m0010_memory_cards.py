"""Migration 0010 — memory_cards table for DPKG Lane C Phase 1.

Adds the ``memory_cards`` table + ``idx_memory_cards_user_filename`` unique
index. Each row represents one parsed card from a user's MEMORY.md file.
Cards are keyed by ``(user_id, filename)`` so the same filename cannot be
registered twice for the same user.

A CHECK constraint on ``category`` enforces the four allowed card kinds:
``user``, ``feedback``, ``project``, ``reference``.

Schema:
    memory_cards(
        id          TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        category    TEXT NOT NULL CHECK(category IN ('user','feedback','project','reference')),
        name        TEXT NOT NULL,
        filename    TEXT NOT NULL,
        description TEXT NOT NULL,
        body        TEXT NOT NULL,
        created_at  TIMESTAMP NOT NULL,
        updated_at  TIMESTAMP NOT NULL
    )
    idx_memory_cards_user_filename UNIQUE ON memory_cards(user_id, filename)
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    "CREATE TABLE IF NOT EXISTS memory_cards ("
    "id TEXT PRIMARY KEY, "
    "user_id TEXT NOT NULL, "
    "category TEXT NOT NULL CHECK(category IN ('user','feedback','project','reference')), "
    "name TEXT NOT NULL, "
    "filename TEXT NOT NULL, "
    "description TEXT NOT NULL, "
    "body TEXT NOT NULL, "
    "created_at TIMESTAMP NOT NULL, "
    "updated_at TIMESTAMP NOT NULL"
    ")",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_cards_user_filename "
    "ON memory_cards(user_id, filename)",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_memory_cards_user_filename")
    conn.execute("DROP TABLE IF EXISTS memory_cards")
