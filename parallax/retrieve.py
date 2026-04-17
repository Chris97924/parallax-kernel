"""Read-side helpers for Parallax.

Thin wrappers over :func:`parallax.sqlite_store.query` that return plain
``dict`` values so callers never have to know about ``sqlite3.Row``. State
filters are applied only when the caller passes a non-``None`` value.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from parallax.sqlite_store import query

__all__ = [
    "memories_by_user",
    "claims_by_user",
    "claims_by_subject",
    "memory_by_content_hash",
    "claim_by_content_hash",
]


def _to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def memories_by_user(
    conn: sqlite3.Connection, user_id: str, state: str | None = None
) -> list[dict]:
    if state is None:
        rows = query(conn, "SELECT * FROM memories WHERE user_id = ?", (user_id,))
    else:
        rows = query(
            conn,
            "SELECT * FROM memories WHERE user_id = ? AND state = ?",
            (user_id, state),
        )
    return _to_dicts(rows)


def claims_by_user(
    conn: sqlite3.Connection, user_id: str, state: str | None = None
) -> list[dict]:
    if state is None:
        rows = query(conn, "SELECT * FROM claims WHERE user_id = ?", (user_id,))
    else:
        rows = query(
            conn,
            "SELECT * FROM claims WHERE user_id = ? AND state = ?",
            (user_id, state),
        )
    return _to_dicts(rows)


def claims_by_subject(
    conn: sqlite3.Connection, user_id: str, subject: str
) -> list[dict]:
    rows = query(
        conn,
        "SELECT * FROM claims WHERE user_id = ? AND subject = ?",
        (user_id, subject),
    )
    return _to_dicts(rows)


def memory_by_content_hash(
    conn: sqlite3.Connection, content_hash: str
) -> Optional[dict]:
    rows = query(
        conn, "SELECT * FROM memories WHERE content_hash = ? LIMIT 1", (content_hash,)
    )
    return dict(rows[0]) if rows else None


def claim_by_content_hash(
    conn: sqlite3.Connection, content_hash: str
) -> Optional[dict]:
    rows = query(
        conn, "SELECT * FROM claims WHERE content_hash = ? LIMIT 1", (content_hash,)
    )
    return dict(rows[0]) if rows else None
