"""Tests for the new ``state=`` kwarg on parallax.ingest.ingest_claim."""

from __future__ import annotations

import sqlite3

import pytest

from parallax.ingest import ingest_claim
from parallax.sqlite_store import query


def test_default_state_is_auto(conn: sqlite3.Connection) -> None:
    cid = ingest_claim(
        conn, user_id="chris", subject="x", predicate="p", object_="o"
    )
    rows = query(conn, "SELECT state FROM claims WHERE claim_id = ?", (cid,))
    assert rows[0]["state"] == "auto"


def test_state_pending_path(conn: sqlite3.Connection) -> None:
    cid = ingest_claim(
        conn,
        user_id="chris",
        subject="x",
        predicate="p",
        object_="o",
        state="pending",
    )
    rows = query(conn, "SELECT state FROM claims WHERE claim_id = ?", (cid,))
    assert rows[0]["state"] == "pending"


def test_illegal_state_raises_value_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="invalid claim state"):
        ingest_claim(
            conn,
            user_id="chris",
            subject="x",
            predicate="p",
            object_="o",
            state="not-a-state",
        )
