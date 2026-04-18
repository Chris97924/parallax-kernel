"""Minimal index_state replay.

Phase-1 stub for the eventual events-based index rebuild. Today
:func:`rebuild_index` recomputes a single ``index_state`` row by counting
the live ``memories`` + ``claims`` rows in the active state, bumps the
version monotonically per ``index_name``, and stamps the watermark to the
most recent ``event_id`` in the events log (so a later replay-from-events
implementation can resume from a known point). Full per-event replay is
deferred to Phase 5.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from parallax.sqlite_store import now_iso

__all__ = ["rebuild_index"]


def _max_version(conn: sqlite3.Connection, index_name: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM index_state WHERE index_name = ?",
        (index_name,),
    ).fetchone()
    return int(row[0])


def _doc_count(conn: sqlite3.Connection) -> int:
    mem = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE state = 'active'"
    ).fetchone()[0]
    cla = conn.execute(
        "SELECT COUNT(*) FROM claims WHERE state IN ('auto','pending','confirmed')"
    ).fetchone()[0]
    return int(mem) + int(cla)


def _last_event_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT event_id FROM events ORDER BY created_at DESC, event_id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


def rebuild_index(conn: sqlite3.Connection, index_name: str) -> dict[str, Any]:
    """Recompute and persist a fresh ``index_state`` row.

    Returns the inserted row as a dict with keys
    ``index_name, version, last_built_at, source_watermark, doc_count,
    state, error_text``. Earlier versions stay in ``index_state`` for
    history; the table's PRIMARY KEY is ``(index_name, version)``.
    """
    version = _max_version(conn, index_name) + 1
    last_built_at = now_iso()
    source_watermark = _last_event_id(conn)
    doc_count = _doc_count(conn)
    state = "ready"
    error_text: str | None = None

    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO index_state
                (index_name, version, last_built_at, source_watermark,
                 doc_count, state, error_text)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                index_name,
                version,
                last_built_at,
                source_watermark,
                doc_count,
                state,
                error_text,
            ),
        )

    return {
        "index_name": index_name,
        "version": version,
        "last_built_at": last_built_at,
        "source_watermark": source_watermark,
        "doc_count": doc_count,
        "state": state,
        "error_text": error_text,
    }
