"""High-level ingest helpers for Parallax.

Wraps :mod:`parallax.sqlite_store` with UPSERT semantics:

* Every direct-input call (``source_id=None``) lazily creates a synthetic
  source row keyed ``direct:<user_id>``. This is what closes the UNIQUE
  NULL-hole on ``claims.source_id`` (schema line reference: see
  ``E:/Parallax/schema.sql``).
* content_hash collisions are absorbed -- the existing row's id is returned
  and no duplicate is written. Re-ingestion is therefore safe and the
  "reaffirm" branch is silent in Phase-0 (see :func:`parallax.sqlite_store.reaffirm`).
"""

from __future__ import annotations

import sqlite3

from ulid import ULID

from parallax.hashing import content_hash
from parallax.sqlite_store import (
    Claim,
    Memory,
    Source,
    insert_claim,
    insert_memory,
    insert_source,
    now_iso,
    query,
)

__all__ = [
    "ingest_memory",
    "ingest_claim",
    "synthetic_direct_source_id",
]


def _ulid() -> str:
    return str(ULID())


def synthetic_direct_source_id(user_id: str) -> str:
    """Return the canonical synthetic source id for direct input."""
    return f"direct:{user_id}"


def _ensure_direct_source(conn: sqlite3.Connection, user_id: str) -> str:
    source_id = synthetic_direct_source_id(user_id)
    insert_source(
        conn,
        Source(
            source_id=source_id,
            uri=f"parallax://direct/{user_id}",
            kind="chat",
            content_hash=content_hash(source_id),
            user_id=user_id,
            ingested_at=now_iso(),
            state="ingested",
        ),
    )
    return source_id


def ingest_memory(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    title: str | None,
    summary: str | None,
    vault_path: str,
    source_id: str | None = None,
) -> str:
    """UPSERT a memory row. Returns the persisted memory_id.

    Race-safe: attempts INSERT OR IGNORE then re-selects by the UNIQUE
    (content_hash, user_id) index so the caller always receives the id of
    the row that is actually persisted, never a ULID that was silently
    dropped by the IGNORE branch.
    """
    if source_id is None:
        source_id = _ensure_direct_source(conn, user_id)

    ch = content_hash(title, summary, vault_path)
    now = now_iso()
    insert_memory(
        conn,
        Memory(
            memory_id=_ulid(),
            user_id=user_id,
            source_id=source_id,
            vault_path=vault_path,
            title=title,
            summary=summary,
            content_hash=ch,
            state="active",
            created_at=now,
            updated_at=now,
        ),
    )
    row = query(
        conn,
        "SELECT memory_id FROM memories WHERE content_hash = ? AND user_id = ?",
        (ch, user_id),
    )
    return row[0]["memory_id"]


def ingest_claim(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    subject: str,
    predicate: str,
    object_: str,
    source_id: str | None = None,
    confidence: float | None = None,
) -> str:
    """UPSERT a claim row. Returns the persisted claim_id.

    Race-safe via INSERT OR IGNORE + re-select on the UNIQUE
    (content_hash, source_id) index. See :func:`ingest_memory` for rationale.
    """
    if source_id is None:
        source_id = _ensure_direct_source(conn, user_id)

    ch = content_hash(subject, predicate, object_, source_id)
    now = now_iso()
    insert_claim(
        conn,
        Claim(
            claim_id=_ulid(),
            user_id=user_id,
            subject=subject,
            predicate=predicate,
            object=object_,
            source_id=source_id,
            content_hash=ch,
            confidence=confidence,
            state="auto",
            created_at=now,
            updated_at=now,
        ),
    )
    row = query(
        conn,
        "SELECT claim_id FROM claims WHERE content_hash = ? AND source_id = ?",
        (ch, source_id),
    )
    return row[0]["claim_id"]
