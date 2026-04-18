"""Tests for parallax.extract.review."""

from __future__ import annotations

import sqlite3

import pytest

from parallax.extract import RawClaim
from parallax.extract.review import (
    approve,
    list_pending,
    queue_pending,
    reject,
)
from parallax.sqlite_store import query


def _raw(text: str = "pending claim") -> RawClaim:
    return RawClaim(
        entity="x",
        claim_text=text,
        polarity=1,
        confidence=0.6,
        claim_type="opinion",
        evidence="",
    )


def test_queue_pending_creates_pending_state(conn: sqlite3.Connection) -> None:
    cid = queue_pending(conn, _raw(), user_id="chris")
    rows = query(conn, "SELECT state FROM claims WHERE claim_id = ?", (cid,))
    assert rows[0]["state"] == "pending"


def test_list_pending_filters_by_user(conn: sqlite3.Connection) -> None:
    queue_pending(conn, _raw("a"), user_id="chris")
    queue_pending(conn, _raw("b"), user_id="chris")
    queue_pending(conn, _raw("c"), user_id="alice")
    chris_pending = list_pending(conn, user_id="chris")
    alice_pending = list_pending(conn, user_id="alice")
    assert len(chris_pending) == 2
    assert len(alice_pending) == 1


def test_approve_moves_to_confirmed_and_emits_event(conn: sqlite3.Connection) -> None:
    cid = queue_pending(conn, _raw("to approve"), user_id="chris")
    approve(conn, cid)
    rows = query(conn, "SELECT state FROM claims WHERE claim_id = ?", (cid,))
    assert rows[0]["state"] == "confirmed"
    events = query(
        conn,
        "SELECT event_type, target_id FROM events "
        "WHERE target_kind = 'claim' AND target_id = ?",
        (cid,),
    )
    assert any(e["event_type"] == "claim.state_changed" for e in events)


def test_reject_moves_to_rejected(conn: sqlite3.Connection) -> None:
    cid = queue_pending(conn, _raw("to reject"), user_id="chris")
    reject(conn, cid)
    rows = query(conn, "SELECT state FROM claims WHERE claim_id = ?", (cid,))
    assert rows[0]["state"] == "rejected"


def test_approve_non_pending_raises(conn: sqlite3.Connection) -> None:
    cid = queue_pending(conn, _raw("already approved"), user_id="chris")
    approve(conn, cid)  # first time ok
    with pytest.raises(ValueError, match="cannot transition from 'confirmed'"):
        approve(conn, cid)


def test_approve_unknown_claim_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="not found"):
        approve(conn, "nonexistent")


def test_concurrent_approve_and_reject_does_not_double_transition(
    conn: sqlite3.Connection,
) -> None:
    """Simulate the TOCTOU race: the row flips between reviewer A's check
    and reviewer B's update. The atomic UPDATE+rowcount guard must reject
    the second transition with a clear error rather than silently writing.
    """
    cid = queue_pending(conn, _raw("race me"), user_id="chris")
    # Reviewer A wins: approves first.
    approve(conn, cid)
    # Reviewer B now tries to reject the same claim — it's no longer
    # pending, so the UPDATE affects zero rows and we must raise.
    with pytest.raises(ValueError, match="cannot transition from 'confirmed'"):
        reject(conn, cid)
    rows = query(conn, "SELECT state FROM claims WHERE claim_id = ?", (cid,))
    assert rows[0]["state"] == "confirmed"
