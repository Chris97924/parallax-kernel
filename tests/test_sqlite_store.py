"""Tests for parallax.sqlite_store.

Contract:
    * Six public functions: insert_source / insert_memory / insert_claim /
      insert_event / query / reaffirm.
    * inserts for sources/memories/claims use ``INSERT OR IGNORE`` to honor
      the ``content_hash`` UNIQUE index (Day-0 dedup).
    * ``insert_event`` is append-only; no update_event / delete_event is
      exported.
    * ``reaffirm`` is a Phase-0 noop.
    * ``query`` returns a list of ``sqlite3.Row`` objects.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax import sqlite_store
from parallax.hashing import content_hash
from parallax.sqlite_store import (
    Claim,
    Event,
    Memory,
    Source,
    connect,
    insert_claim,
    insert_event,
    insert_memory,
    insert_source,
    now_iso,
    query,
    reaffirm,
)


def _now() -> str:
    return now_iso()


@pytest.fixture()
def seeded_source(conn: sqlite3.Connection) -> Source:
    src = Source(
        source_id="direct:chris",
        uri="parallax://direct/chris",
        kind="chat",
        content_hash=content_hash("direct:chris"),
        user_id="chris",
        ingested_at=_now(),
        state="ingested",
    )
    insert_source(conn, src)
    return src


class TestPublicSurface:
    def test_all_exports_exact(self) -> None:
        expected = {
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
        }
        assert set(sqlite_store.__all__) == expected

    def test_no_update_or_delete_event(self) -> None:
        assert not hasattr(sqlite_store, "update_event")
        assert not hasattr(sqlite_store, "delete_event")


class TestConnect:
    def test_row_factory_and_foreign_keys(self, tmp_path: pathlib.Path) -> None:
        c = connect(tmp_path / "x.db")
        assert c.row_factory is sqlite3.Row
        fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        c.close()


class TestInsertAndDedup:
    def test_insert_source_roundtrip(self, conn: sqlite3.Connection, seeded_source: Source) -> None:
        rows = query(conn, "SELECT * FROM sources WHERE source_id = ?", (seeded_source.source_id,))
        assert len(rows) == 1
        assert rows[0]["uri"] == seeded_source.uri

    def test_insert_memory_dedup_via_content_hash(
        self, conn: sqlite3.Connection, seeded_source: Source
    ) -> None:
        ch = content_hash("title", "summary", "vault/foo.md")
        mem = Memory(
            memory_id="01HXMEM0000000000000000001",
            user_id="chris",
            source_id=seeded_source.source_id,
            vault_path="vault/foo.md",
            title="title",
            summary="summary",
            content_hash=ch,
            state="active",
            created_at=_now(),
            updated_at=_now(),
        )
        insert_memory(conn, mem)
        # second insert with different memory_id but same content_hash -> ignored
        dup = Memory(**{**mem.__dict__, "memory_id": "01HXMEM0000000000000000002"})
        insert_memory(conn, dup)
        rows = query(conn, "SELECT * FROM memories WHERE content_hash = ?", (ch,))
        assert len(rows) == 1
        assert rows[0]["memory_id"] == "01HXMEM0000000000000000001"

    def test_insert_claim_dedup_via_content_hash(
        self, conn: sqlite3.Connection, seeded_source: Source
    ) -> None:
        ch = content_hash("chris", "likes", "coffee", seeded_source.source_id)
        claim = Claim(
            claim_id="01HXCLAIM0000000000000001",
            user_id="chris",
            subject="chris",
            predicate="likes",
            object="coffee",
            source_id=seeded_source.source_id,
            content_hash=ch,
            confidence=0.9,
            state="auto",
            created_at=_now(),
            updated_at=_now(),
        )
        insert_claim(conn, claim)
        dup = Claim(**{**claim.__dict__, "claim_id": "01HXCLAIM0000000000000002"})
        insert_claim(conn, dup)
        rows = query(
            conn,
            "SELECT * FROM claims WHERE content_hash = ? AND source_id = ?",
            (ch, seeded_source.source_id),
        )
        assert len(rows) == 1


class TestInsertEventAppendOnly:
    def test_events_append_on_duplicate(self, conn: sqlite3.Connection) -> None:
        ev1 = Event(
            event_id="01HXEV00000000000000000001",
            user_id="chris",
            actor="system",
            event_type="memory.created",
            target_kind="memory",
            target_id="m1",
            payload_json='{"x":1}',
            approval_tier=None,
            created_at=_now(),
        )
        ev2 = Event(**{**ev1.__dict__, "event_id": "01HXEV00000000000000000002"})
        insert_event(conn, ev1)
        insert_event(conn, ev2)
        rows = query(conn, "SELECT * FROM events ORDER BY event_id", ())
        assert len(rows) == 2
        assert rows[0]["event_id"] == "01HXEV00000000000000000001"
        assert rows[1]["event_id"] == "01HXEV00000000000000000002"


class TestQuery:
    def test_query_returns_row_list(self, conn: sqlite3.Connection) -> None:
        rows = query(conn, "SELECT 1 AS one, 'hi' AS two", ())
        assert isinstance(rows, list)
        assert isinstance(rows[0], sqlite3.Row)
        assert rows[0]["one"] == 1
        assert rows[0]["two"] == "hi"

    def test_query_parameterized(self, conn: sqlite3.Connection, seeded_source: Source) -> None:
        rows = query(conn, "SELECT source_id FROM sources WHERE user_id = ?", ("chris",))
        assert len(rows) == 1


class TestReaffirmSurface:
    """v0.4.0: reaffirm() is a typed public facade over record_event."""

    def _seed_memory(self, conn: sqlite3.Connection, seeded_source: Source) -> str:
        mem = Memory(
            memory_id="mem-1",
            user_id="chris",
            source_id=seeded_source.source_id,
            vault_path="v.md",
            title="t",
            summary="s",
            content_hash=content_hash("t", "s", "v.md"),
            state="active",
            created_at=_now(),
            updated_at=_now(),
        )
        insert_memory(conn, mem)
        return mem.memory_id

    def _seed_claim(self, conn: sqlite3.Connection, seeded_source: Source) -> str:
        cla = Claim(
            claim_id="cla-1",
            user_id="chris",
            subject="x",
            predicate="y",
            object="z",
            source_id=seeded_source.source_id,
            content_hash=content_hash("x", "y", "z", seeded_source.source_id),
            confidence=None,
            state="auto",
            created_at=_now(),
            updated_at=_now(),
        )
        insert_claim(conn, cla)
        return cla.claim_id

    def test_memory_kind_emits_memory_reaffirmed(
        self, conn: sqlite3.Connection, seeded_source: Source
    ) -> None:
        mid = self._seed_memory(conn, seeded_source)
        eid = reaffirm(conn, user_id="chris", kind="memory", entity_id=mid)
        assert isinstance(eid, str) and len(eid) > 0
        rows = query(
            conn,
            "SELECT event_type FROM events WHERE event_id = ?",
            (eid,),
        )
        assert len(rows) == 1
        assert rows[0]["event_type"] == "memory.reaffirmed"

    def test_claim_kind_emits_claim_reaffirmed(
        self, conn: sqlite3.Connection, seeded_source: Source
    ) -> None:
        cid = self._seed_claim(conn, seeded_source)
        eid = reaffirm(conn, user_id="chris", kind="claim", entity_id=cid)
        assert isinstance(eid, str) and len(eid) > 0
        rows = query(
            conn,
            "SELECT event_type, target_id FROM events WHERE event_id = ?",
            (eid,),
        )
        assert len(rows) == 1
        assert rows[0]["event_type"] == "claim.reaffirmed"
        assert rows[0]["target_id"] == cid

    def test_invalid_kind_raises_value_error(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="memory"):
            reaffirm(conn, user_id="chris", kind="source", entity_id="s")

    def test_missing_required_kw_raises_type_error(
        self, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(TypeError):
            reaffirm(conn, user_id="chris", kind="memory")  # type: ignore[call-arg]
