"""SQLite storage layer for Parallax.

This module is the only writer that touches the SQLite canonical store. It
exposes a small, intentionally narrow surface:

    insert_source / insert_memory / insert_claim   -- dedup via content_hash
    insert_event                                   -- append-only
    query                                          -- read via parameterized SQL
    reaffirm                                       -- emit <kind>.reaffirmed event

The events table is write-only from this layer: there is NO update_event and
NO delete_event export. Enforcing the contract here keeps the append-only
semantics intact without needing DB-level triggers in Phase-0.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import pathlib
import sqlite3
from collections.abc import Sequence
from typing import Any

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
    """Return the current UTC time as an ISO-8601 string.

    Always includes microsecond precision (``timespec='microseconds'``) so
    stored ``created_at`` strings have a stable length and field layout. The
    retrieval window in :func:`parallax.retrieve.by_timeline` relies on this
    invariant: its ``_iso_normalize`` emits ``'.SSSSSS+00:00'`` bounds, and
    any stored row lacking the microsecond component would break
    lex-compare at position 19 (``'+'`` vs ``'.'``) and silently drop out
    of the window (BUG 1/4, v0.5.0-pre1).
    """
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="microseconds")


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
    session_id: str | None = None


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
    """Insert a claim row; dedup via ``UNIQUE(content_hash, source_id, user_id)``
    (ADR-005, v0.5.0-pre1)."""
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
    """Append an event row. Events are write-only by contract.

    session_id column exists from migration 0006 onwards; pre-0006 DBs
    will reject the INSERT with OperationalError, so callers running
    against older schemas must migrate_to_latest first.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO events
                (event_id, user_id, actor, event_type, target_kind, target_id,
                 payload_json, approval_tier, created_at, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


_VALID_REAFFIRM_KINDS = ("memory", "claim")


def reaffirm(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    kind: str,
    entity_id: str,
    actor: str = "system",
) -> str:
    """Emit a ``<kind>.reaffirmed`` event and return its event_id.

    v0.4.0 typed surface. ``kind`` must be one of
    :data:`_VALID_REAFFIRM_KINDS` (``"memory"`` or ``"claim"``); any other
    value raises :class:`ValueError` listing the valid kinds.

    For ``kind='memory'`` this delegates to
    :func:`parallax.events.record_memory_reaffirmed` so ``memory.reaffirmed``
    stays the single stable public name. For ``kind='claim'`` it emits a
    ``claim.reaffirmed`` event via :func:`parallax.events.record_event`.
    """
    if kind not in _VALID_REAFFIRM_KINDS:
        raise ValueError(
            f"invalid reaffirm kind {kind!r}; "
            f"expected one of {list(_VALID_REAFFIRM_KINDS)}"
        )
    from parallax.events import record_event, record_memory_reaffirmed

    if kind == "memory":
        return record_memory_reaffirmed(
            conn, user_id=user_id, memory_id=entity_id, actor=actor
        )
    return record_event(
        conn,
        user_id=user_id,
        actor=actor,
        event_type="claim.reaffirmed",
        target_kind="claim",
        target_id=entity_id,
        payload={"claim_id": entity_id},
    )
