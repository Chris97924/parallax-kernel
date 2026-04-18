"""Review queue — thin wrapper over claim.state transitions.

The a2a design wrote pending claims to a separate ``Pending.md`` table.
Parallax already models claim lifecycle via ``parallax.transitions``;
``state='pending'`` is the canonical equivalent. This module exposes the
three operations a review UI needs (queue / list / approve / reject) and
emits ``claim.state_changed`` events so the audit trail is DB-native.
"""

from __future__ import annotations

import sqlite3
from typing import Any, TypedDict

from parallax.events import record_claim_state_changed
from parallax.extract.ingest import claim_predicate
from parallax.extract.types import RawClaim
from parallax.ingest import ingest_claim
from parallax.retrieve import claims_by_user
from parallax.transitions import is_allowed_transition

__all__ = ["PendingClaim", "queue_pending", "list_pending", "approve", "reject"]


class PendingClaim(TypedDict, total=False):
    """Row shape returned by :func:`list_pending`.

    Fields mirror the ``claims`` table columns; ``total=False`` leaves room
    for schema additions without breaking existing consumers.
    """

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


def queue_pending(
    conn: sqlite3.Connection,
    claim: RawClaim,
    *,
    user_id: str,
    source_id: str | None = None,
) -> str:
    """Insert ``claim`` with ``state='pending'`` and return its claim_id."""
    return ingest_claim(
        conn,
        user_id=user_id,
        subject=claim.entity,
        predicate=claim_predicate(claim),
        object_=claim.claim_text,
        source_id=source_id,
        confidence=claim.confidence,
        state="pending",
    )


def list_pending(conn: sqlite3.Connection, *, user_id: str) -> list[PendingClaim]:
    """Return every pending claim for ``user_id``.

    Rows conform to :class:`PendingClaim`. Callers that only need the claim
    ids can map ``row["claim_id"]`` over the result.
    """
    rows: list[dict[str, Any]] = claims_by_user(conn, user_id, state="pending")
    return [PendingClaim(**row) for row in rows]  # type: ignore[typeddict-item]


def _transition(
    conn: sqlite3.Connection, *, claim_id: str, to_state: str
) -> None:
    """Atomically move a pending claim to ``to_state``.

    A single ``UPDATE ... WHERE state='pending'`` + rowcount check closes
    the TOCTOU window between a prior SELECT and the UPDATE: if a
    concurrent reviewer flipped the claim first, rowcount is zero and we
    raise, so no double-approve / approve+reject race can slip through.
    """
    if not is_allowed_transition("claim", "pending", to_state):
        raise ValueError(
            f"transition 'pending' -> {to_state!r} not allowed for claims"
        )
    with conn:
        cursor = conn.execute(
            "UPDATE claims SET state = ?, updated_at = datetime('now') "
            "WHERE claim_id = ? AND state = 'pending'",
            (to_state, claim_id),
        )
        if cursor.rowcount == 0:
            # Either the row doesn't exist or it is already past 'pending'.
            existing = conn.execute(
                "SELECT state FROM claims WHERE claim_id = ? LIMIT 1",
                (claim_id,),
            ).fetchone()
            if existing is None:
                raise ValueError(f"claim {claim_id!r} not found")
            raise ValueError(
                f"claim {claim_id!r} cannot transition from {existing[0]!r} "
                f"(review.{to_state} requires state='pending')"
            )
        user_row = conn.execute(
            "SELECT user_id FROM claims WHERE claim_id = ? LIMIT 1",
            (claim_id,),
        ).fetchone()
        record_claim_state_changed(
            conn,
            user_id=user_row[0],
            claim_id=claim_id,
            from_state="pending",
            to_state=to_state,
        )


def approve(conn: sqlite3.Connection, claim_id: str) -> None:
    """Transition a pending claim to ``state='confirmed'`` with audit event."""
    _transition(conn, claim_id=claim_id, to_state="confirmed")


def reject(conn: sqlite3.Connection, claim_id: str) -> None:
    """Transition a pending claim to ``state='rejected'`` with audit event."""
    _transition(conn, claim_id=claim_id, to_state="rejected")
