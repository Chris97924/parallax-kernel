"""Tests for parallax.events — record_event + helpers + ingest wiring."""

from __future__ import annotations

import json
import sqlite3

import pytest

from parallax.events import (
    record_claim_state_changed,
    record_event,
    record_memory_reaffirmed,
)
from parallax.ingest import ingest_claim, ingest_memory


def _events(conn: sqlite3.Connection, event_type: str | None = None) -> list[sqlite3.Row]:
    if event_type is None:
        return conn.execute("SELECT * FROM events").fetchall()
    return conn.execute(
        "SELECT * FROM events WHERE event_type = ?", (event_type,)
    ).fetchall()


class TestRecordEvent:
    def test_happy_path_returns_event_id(self, conn: sqlite3.Connection) -> None:
        eid = record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="audit.test",
            target_kind=None,
            target_id=None,
            payload={"k": "v"},
        )
        assert isinstance(eid, str) and len(eid) >= 16
        rows = _events(conn, "audit.test")
        assert len(rows) == 1
        assert rows[0]["event_id"] == eid
        assert json.loads(rows[0]["payload_json"]) == {"k": "v"}

    def test_orphan_target_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="orphan event rejected"):
            record_event(
                conn,
                user_id="u",
                actor="system",
                event_type="memory.touched",
                target_kind="memory",
                target_id="never-existed",
                payload=None,
            )
        # No row was written
        assert _events(conn) == []

    def test_target_pair_must_be_consistent(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="must be provided together"):
            record_event(
                conn,
                user_id="u",
                actor="system",
                event_type="bad",
                target_kind="memory",
                target_id=None,
            )

    def test_payload_round_trip_sorted_keys(self, conn: sqlite3.Connection) -> None:
        eid = record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="audit.payload",
            target_kind=None,
            target_id=None,
            payload={"b": 2, "a": 1},
        )
        row = conn.execute(
            "SELECT payload_json FROM events WHERE event_id = ?", (eid,)
        ).fetchone()
        # sort_keys=True so 'a' precedes 'b' in the serialized form
        assert row[0] == '{"a": 1, "b": 2}'

    def test_valid_target_passes(self, conn: sqlite3.Connection) -> None:
        mid = ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        eid = record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="memory.touched",
            target_kind="memory",
            target_id=mid,
            payload={"memory_id": mid},
        )
        assert _events(conn, "memory.touched")[0]["event_id"] == eid


class TestRecordMemoryReaffirmed:
    def test_helper_writes_correct_event_type(self, conn: sqlite3.Connection) -> None:
        mid = ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        record_memory_reaffirmed(conn, user_id="u", memory_id=mid)
        rows = _events(conn, "memory.reaffirmed")
        assert len(rows) == 1
        assert rows[0]["target_kind"] == "memory"
        assert rows[0]["target_id"] == mid


class TestRecordClaimStateChanged:
    def test_helper_writes_from_to_payload(self, conn: sqlite3.Connection) -> None:
        cid = ingest_claim(
            conn, user_id="u", subject="x", predicate="y", object_="z"
        )
        record_claim_state_changed(
            conn, user_id="u", claim_id=cid, from_state="auto", to_state="confirmed"
        )
        rows = _events(conn, "claim.state_changed")
        assert len(rows) == 1
        assert json.loads(rows[0]["payload_json"]) == {
            "from": "auto",
            "to": "confirmed",
        }
        assert rows[0]["actor"] == "system"


class TestIngestEmitsReaffirmed:
    def test_first_ingest_emits_no_reaffirmed(self, conn: sqlite3.Connection) -> None:
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        assert _events(conn, "memory.reaffirmed") == []

    def test_second_ingest_emits_one_reaffirmed(self, conn: sqlite3.Connection) -> None:
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        rows = _events(conn, "memory.reaffirmed")
        assert len(rows) == 1

    def test_third_ingest_emits_two_total(self, conn: sqlite3.Connection) -> None:
        for _ in range(3):
            ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        rows = _events(conn, "memory.reaffirmed")
        assert len(rows) == 2
