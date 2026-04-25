"""Full events-based replay for Parallax.

v0.4.0 Phase-3 deliverable: the events log is sufficient to rebuild
``claims`` and ``memories`` bit-for-bit into an empty (schema-only) DB.

Two entry points:

``replay_events(conn, *, into_conn=None)``
    Reads events in (created_at ASC, event_id ASC) order and applies them
    to the target connection. When ``into_conn`` is None, events are
    replayed in-place (useful for tests); otherwise events are read from
    ``conn`` and rows written into ``into_conn`` — the production rebuild
    path.

``backfill_creation_events(conn)``
    For every row in ``claims`` / ``memories`` that lacks a matching
    ``<kind>.created`` event, synthesize one carrying the full row
    payload. Makes pre-0.4.0 DBs (which wrote rows directly without
    emitting create events) replayable. Idempotent.

Event dispatch:

    memory.created / claim.created  → INSERT into memories/claims
    claim.state_changed             → UPDATE claims SET state = payload.to,
                                             updated_at = payload.updated_at
    memory.state_changed            → UPDATE memories SET state = payload.to,
                                             updated_at = payload.updated_at
    memory.reaffirmed / claim.reaffirmed
                                    → counted, no row mutation
    all other event_types           → skipped (session.*, tool.*, prompt.*,
                                      file.edit, audit.*, etc.)
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3

from parallax.sqlite_store import Claim, Memory, insert_claim, insert_memory

__all__ = [
    "ReplaySummary",
    "BackfillSummary",
    "replay_events",
    "backfill_creation_events",
]

_MEMORY_CREATED = "memory.created"
_CLAIM_CREATED = "claim.created"
_CLAIM_STATE_CHANGED = "claim.state_changed"
_MEMORY_STATE_CHANGED = "memory.state_changed"
_MEMORY_REAFFIRMED = "memory.reaffirmed"
_CLAIM_REAFFIRMED = "claim.reaffirmed"

_MEMORY_FIELDS = (
    "memory_id",
    "user_id",
    "source_id",
    "vault_path",
    "title",
    "summary",
    "content_hash",
    "state",
    "created_at",
    "updated_at",
)

_CLAIM_FIELDS = (
    "claim_id",
    "user_id",
    "subject",
    "predicate",
    "object",
    "source_id",
    "content_hash",
    "confidence",
    "state",
    "created_at",
    "updated_at",
)


@dataclasses.dataclass(frozen=True)
class ReplaySummary:
    memories_rebuilt: int
    claims_rebuilt: int
    claim_state_updates: int
    memory_state_updates: int
    events_consumed: int
    events_skipped: int
    skipped_event_types: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class BackfillSummary:
    memory_creations_added: int
    claim_creations_added: int


def _apply_memory_created(conn: sqlite3.Connection, payload: dict) -> bool:
    missing = [f for f in _MEMORY_FIELDS if f not in payload]
    if missing:
        return False
    mem = Memory(**{f: payload[f] for f in _MEMORY_FIELDS})
    insert_memory(conn, mem)
    return True


def _apply_claim_created(conn: sqlite3.Connection, payload: dict) -> bool:
    missing = [f for f in _CLAIM_FIELDS if f not in payload]
    if missing:
        return False
    cla = Claim(**{f: payload[f] for f in _CLAIM_FIELDS})
    insert_claim(conn, cla)
    return True


def _apply_claim_state_changed(conn: sqlite3.Connection, target_id: str, payload: dict) -> bool:
    to_state = payload.get("to")
    if not to_state or not target_id:
        return False
    updated_at = payload.get("updated_at")
    with conn:
        if updated_at is not None:
            conn.execute(
                "UPDATE claims SET state = ?, updated_at = ? WHERE claim_id = ?",
                (to_state, updated_at, target_id),
            )
        else:
            conn.execute(
                "UPDATE claims SET state = ? WHERE claim_id = ?",
                (to_state, target_id),
            )
    return True


def _apply_memory_state_changed(conn: sqlite3.Connection, target_id: str, payload: dict) -> bool:
    to_state = payload.get("to")
    if not to_state or not target_id:
        return False
    updated_at = payload.get("updated_at")
    with conn:
        if updated_at is not None:
            conn.execute(
                "UPDATE memories SET state = ?, updated_at = ? WHERE memory_id = ?",
                (to_state, updated_at, target_id),
            )
        else:
            conn.execute(
                "UPDATE memories SET state = ? WHERE memory_id = ?",
                (to_state, target_id),
            )
    return True


def replay_events(
    conn: sqlite3.Connection,
    *,
    into_conn: sqlite3.Connection | None = None,
) -> ReplaySummary:
    """Replay the events log from ``conn`` onto ``into_conn`` (or ``conn``).

    Returns a :class:`ReplaySummary` with per-kind counts. Unknown
    event_types are skipped, not raised.
    """
    target = into_conn if into_conn is not None else conn
    memories_rebuilt = 0
    claims_rebuilt = 0
    claim_state_updates = 0
    memory_state_updates = 0
    events_consumed = 0
    events_skipped = 0
    skipped: set[str] = set()

    cur = conn.execute("""
        SELECT event_type, target_id, payload_json
        FROM events
        ORDER BY created_at ASC, event_id ASC
        """)
    # In-place replay (into_conn is None) reads and writes on the same
    # connection; buffer rows first so write-side commits don't invalidate
    # the open read cursor.  Cross-connection replay streams to stay O(1).
    rows = cur.fetchall() if into_conn is None else cur
    for row in rows:
        events_consumed += 1
        event_type = row["event_type"]
        target_id = row["target_id"]
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except json.JSONDecodeError:
            payload = {}

        if event_type == _MEMORY_CREATED:
            if _apply_memory_created(target, payload):
                memories_rebuilt += 1
            else:
                events_skipped += 1
                skipped.add(event_type)
        elif event_type == _CLAIM_CREATED:
            if _apply_claim_created(target, payload):
                claims_rebuilt += 1
            else:
                events_skipped += 1
                skipped.add(event_type)
        elif event_type == _CLAIM_STATE_CHANGED:
            if _apply_claim_state_changed(target, target_id, payload):
                claim_state_updates += 1
            else:
                events_skipped += 1
                skipped.add(event_type)
        elif event_type == _MEMORY_STATE_CHANGED:
            if _apply_memory_state_changed(target, target_id, payload):
                memory_state_updates += 1
            else:
                events_skipped += 1
                skipped.add(event_type)
        elif event_type in (_MEMORY_REAFFIRMED, _CLAIM_REAFFIRMED):
            # Reaffirmation is counted but does not mutate rows.
            pass
        else:
            events_skipped += 1
            skipped.add(event_type)

    return ReplaySummary(
        memories_rebuilt=memories_rebuilt,
        claims_rebuilt=claims_rebuilt,
        claim_state_updates=claim_state_updates,
        memory_state_updates=memory_state_updates,
        events_consumed=events_consumed,
        events_skipped=events_skipped,
        skipped_event_types=tuple(sorted(skipped)),
    )


def _memories_missing_created(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT m.*
        FROM memories m
        WHERE NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.event_type = 'memory.created'
              AND e.target_kind = 'memory'
              AND e.target_id = m.memory_id
        )
        """).fetchall()


def _claims_missing_created(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT c.*
        FROM claims c
        WHERE NOT EXISTS (
            SELECT 1 FROM events e
            WHERE e.event_type = 'claim.created'
              AND e.target_kind = 'claim'
              AND e.target_id = c.claim_id
        )
        """).fetchall()


def backfill_creation_events(conn: sqlite3.Connection) -> BackfillSummary:
    """Synthesize create events for pre-0.4.0 rows.

    For every ``memories`` / ``claims`` row without a matching
    ``memory.created`` / ``claim.created`` event, insert one carrying the
    full row payload. Uses the row's own ``created_at`` as the event's
    ``created_at`` so a subsequent replay preserves chronological order
    against any co-existing state_changed events.

    Idempotent: a second call is a no-op because the NOT EXISTS guard
    excludes rows that already have a create event.
    """
    from ulid import ULID

    mem_added = 0
    claim_added = 0

    for row in _memories_missing_created(conn):
        payload = {f: row[f] for f in _MEMORY_FIELDS}
        event_id = str(ULID())
        conn.execute(
            """
            INSERT INTO events
                (event_id, user_id, actor, event_type, target_kind, target_id,
                 payload_json, approval_tier, created_at, session_id)
            VALUES (?, ?, 'system:backfill', 'memory.created', 'memory', ?, ?,
                    NULL, ?, NULL)
            """,
            (
                event_id,
                row["user_id"],
                row["memory_id"],
                json.dumps(payload, sort_keys=True),
                row["created_at"],
            ),
        )
        mem_added += 1

    for row in _claims_missing_created(conn):
        payload = {f: row[f] for f in _CLAIM_FIELDS}
        event_id = str(ULID())
        conn.execute(
            """
            INSERT INTO events
                (event_id, user_id, actor, event_type, target_kind, target_id,
                 payload_json, approval_tier, created_at, session_id)
            VALUES (?, ?, 'system:backfill', 'claim.created', 'claim', ?, ?,
                    NULL, ?, NULL)
            """,
            (
                event_id,
                row["user_id"],
                row["claim_id"],
                json.dumps(payload, sort_keys=True),
                row["created_at"],
            ),
        )
        claim_added += 1

    conn.commit()
    return BackfillSummary(
        memory_creations_added=mem_added,
        claim_creations_added=claim_added,
    )
