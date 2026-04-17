"""SQLite storage layer for Parallax.

This module is the only writer that touches the SQLite canonical store. It
exposes a small, intentionally narrow surface:

    insert_source / insert_memory / insert_claim   -- dedup via content_hash
    insert_event                                   -- append-only
    query                                          -- read via parameterized SQL
    reaffirm                                       -- Phase-0 noop placeholder

The events table is write-only from this layer: there is NO update_event and
NO delete_event export. Enforcing the contract here keeps the append-only
semantics intact without needing DB-level triggers in Phase-0.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import pathlib
import sqlite3
from typing import Any, Sequence

__all__ = [
    "insert_source",
    "insert_memory",
    "insert_claim",
    "insert_event",
    "query",
    "reaffirm",
    "connect",
    "now_iso",
    "Source",
    "Memory",
    "Claim",
    "Event",
]


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ----- Record dataclasses ---------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Source:
    source_id: str
    uri: str
    kind: str
    content_hash: str
    user_id: str
    ingested_at: str
    state: str


@dataclasses.dataclass(frozen=True)
class Memory:
    memory_id: str
    user_id: str
    source_id: str | None
    vault_path: str
    title: str | None
    summary: str | None
    content_hash: str
    state: str
    created_at: str
    updated_at: str


@dataclasses.dataclass(frozen=True)
class Claim:
    claim_id: str
    user_id: str
    subject: str
    predicate: str
    object: str
    source_id: str
    content_hash: str
    confidence: float | None
    state: str
    created_at: str
    updated_at: str


@dataclasses.dataclass(frozen=True)
class Event:
    event_id: str
    user_id: str
    actor: str
    event_type: str
    target_kind: str | None
    target_id: str | None
    payload_json: str
    approval_tier: str | None
    created_at: str


# ----- Connection helper ----------------------------------------------------


def connect(db_path: pathlib.Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with Row factory + foreign keys on."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ----- Writers --------------------------------------------------------------


def insert_source(conn: sqlite3.Connection, source: Source) -> None:
    """Insert a source row; dedup via the ``source_id`` primary key."""
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO sources
                (source_id, uri, kind, content_hash, user_id, ingested_at, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            dataclasses.astuple(source),
        )


def insert_memory(conn: sqlite3.Connection, memory: Memory) -> None:
    """Insert a memory row; dedup via ``UNIQUE(content_hash, user_id)``."""
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO memories
                (memory_id, user_id, source_id, vault_path, title, summary,
                 content_hash, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            dataclasses.astuple(memory),
        )


def insert_claim(conn: sqlite3.Connection, claim: Claim) -> None:
    """Insert a claim row; dedup via ``UNIQUE(content_hash, source_id)``."""
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO claims
                (claim_id, user_id, subject, predicate, object, source_id,
                 content_hash, confidence, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            dataclasses.astuple(claim),
        )


def insert_event(conn: sqlite3.Connection, event: Event) -> None:
    """Append an event row. Events are write-only by contract."""
    with conn:
        conn.execute(
            """
            INSERT INTO events
                (event_id, user_id, actor, event_type, target_kind, target_id,
                 payload_json, approval_tier, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            dataclasses.astuple(event),
        )


# ----- Reader ---------------------------------------------------------------


def query(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> list[sqlite3.Row]:
    """Execute a parameterized SELECT and return all rows as a list."""
    cur = conn.execute(sql, tuple(params))
    return cur.fetchall()


# ----- Placeholder ----------------------------------------------------------


def reaffirm(conn: sqlite3.Connection, *args: Any, **kwargs: Any) -> None:
    """Phase-0 noop.

    Real reaffirm semantics (emit a ``*.reaffirmed`` event when an UPSERT
    collapses onto an existing row) land in Phase-1 once the events append
    trigger is in place. Kept in the public surface so callers can already
    wire the hook without import churn later.
    """
    return None
