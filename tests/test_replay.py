"""Tests for parallax.replay — events → rows bit-for-bit rebuild."""

from __future__ import annotations

import pathlib
import sqlite3

from parallax.events import record_claim_state_changed
from parallax.hashing import content_hash
from parallax.ingest import ingest_claim, ingest_memory
from parallax.migrations import migrate_to_latest
from parallax.replay import (
    BackfillSummary,
    ReplaySummary,
    backfill_creation_events,
    replay_events,
)
from parallax.sqlite_store import Claim, Memory, connect, insert_claim, insert_memory, now_iso


def _fresh_conn(tmp_path: pathlib.Path, name: str) -> sqlite3.Connection:
    db = tmp_path / name
    c = connect(db)
    migrate_to_latest(c)
    return c


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(sql).fetchall()]


def _seed_source(conn: sqlite3.Connection, source_id: str, user_id: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO sources(source_id, uri, kind, content_hash,
                                         user_id, ingested_at, state)
           VALUES (?, ?, 'file', ?, ?, datetime('now'), 'ingested')""",
        (source_id, f"file://{source_id}", f"hash-{source_id}", user_id),
    )
    conn.commit()


class TestReplayRebuild:
    def test_fresh_replay_reproduces_rows(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        for i in range(5):
            ingest_memory(
                conn,
                user_id="u",
                title=f"t{i}",
                summary=f"s{i}",
                vault_path=f"v{i}.md",
            )
        for i in range(5):
            ingest_claim(
                conn,
                user_id="u",
                subject=f"s{i}",
                predicate="likes",
                object_=f"o{i}",
            )
        src_mems = _rows(conn, "SELECT * FROM memories ORDER BY memory_id")
        src_claims = _rows(conn, "SELECT * FROM claims ORDER BY claim_id")

        fresh = _fresh_conn(tmp_path, "fresh.db")
        # Seed the synthetic source too — ingest_memory/claim created it in
        # src but replay only touches memories/claims so sources must be
        # seeded in fresh for FK constraints to hold.
        _seed_source(fresh, "direct:u", "u")

        summary = replay_events(conn, into_conn=fresh)

        assert isinstance(summary, ReplaySummary)
        assert summary.memories_rebuilt == 5
        assert summary.claims_rebuilt == 5

        assert _rows(fresh, "SELECT * FROM memories ORDER BY memory_id") == src_mems
        assert _rows(fresh, "SELECT * FROM claims ORDER BY claim_id") == src_claims
        fresh.close()

    def test_state_change_applied_in_order(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        cid = ingest_claim(
            conn, user_id="u", subject="x", predicate="y", object_="z"
        )
        record_claim_state_changed(
            conn, user_id="u", claim_id=cid, from_state="auto", to_state="confirmed"
        )
        fresh = _fresh_conn(tmp_path, "state.db")
        _seed_source(fresh, "direct:u", "u")
        replay_events(conn, into_conn=fresh)
        row = fresh.execute(
            "SELECT state FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()
        assert row["state"] == "confirmed"
        fresh.close()

    def test_claim_review_updated_at_survives_replay(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        # Exercise the extract/review.py code path: it performs
        # UPDATE claims SET state=?, updated_at=? AND emits a state_changed
        # event carrying updated_at in the payload. Replay MUST reproduce
        # both columns bit-for-bit (US-005 acceptance criterion).
        from parallax.extract.review import approve
        cid = ingest_claim(
            conn, user_id="u", subject="x", predicate="y", object_="z",
            state="pending",
        )
        approve(conn, cid)
        src_row = conn.execute(
            "SELECT state, updated_at FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()
        assert src_row["state"] == "confirmed"
        assert src_row["updated_at"]

        fresh = _fresh_conn(tmp_path, "review-replay.db")
        _seed_source(fresh, "direct:u", "u")
        replay_events(conn, into_conn=fresh)
        fresh_row = fresh.execute(
            "SELECT state, updated_at FROM claims WHERE claim_id = ?", (cid,)
        ).fetchone()
        assert fresh_row["state"] == src_row["state"]
        assert fresh_row["updated_at"] == src_row["updated_at"]
        fresh.close()

    def test_memory_state_change_updated_at_survives_replay(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        # Synthesize a memory.state_changed event with explicit updated_at
        # to lock the memory branch of the replay handler.
        from parallax.events import record_event
        mid = ingest_memory(
            conn, user_id="u", title="t", summary="s", vault_path="v.md"
        )
        new_updated_at = "2099-12-31T23:59:59.000000+00:00"
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="memory.state_changed",
            target_kind="memory",
            target_id=mid,
            payload={"from": "active", "to": "archived",
                     "updated_at": new_updated_at},
        )
        conn.execute(
            "UPDATE memories SET state = ?, updated_at = ? WHERE memory_id = ?",
            ("archived", new_updated_at, mid),
        )
        conn.commit()

        fresh = _fresh_conn(tmp_path, "mem-state.db")
        _seed_source(fresh, "direct:u", "u")
        replay_events(conn, into_conn=fresh)
        fresh_row = fresh.execute(
            "SELECT state, updated_at FROM memories WHERE memory_id = ?", (mid,)
        ).fetchone()
        assert fresh_row["state"] == "archived"
        assert fresh_row["updated_at"] == new_updated_at
        fresh.close()

    def test_unknown_event_type_is_skipped_not_raised(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        # Insert a garbage event directly
        conn.execute(
            """INSERT INTO events(event_id, user_id, actor, event_type,
                                  target_kind, target_id, payload_json,
                                  created_at)
               VALUES ('evt-x', 'u', 'system', 'custom.unknown', NULL, NULL,
                       '{}', datetime('now'))"""
        )
        conn.commit()
        fresh = _fresh_conn(tmp_path, "skip.db")
        _seed_source(fresh, "direct:u", "u")
        summary = replay_events(conn, into_conn=fresh)
        assert "custom.unknown" in summary.skipped_event_types
        assert summary.memories_rebuilt == 1
        fresh.close()

    def test_reaffirmed_events_are_consumed_not_skipped(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        fresh = _fresh_conn(tmp_path, "reaff.db")
        _seed_source(fresh, "direct:u", "u")
        summary = replay_events(conn, into_conn=fresh)
        # memory.reaffirmed is a no-op for row rebuild but is consumed
        assert "memory.reaffirmed" not in summary.skipped_event_types
        fresh.close()


class TestBackfillCreationEvents:
    def _insert_raw_memory(
        self, conn: sqlite3.Connection, vault: str
    ) -> Memory:
        _seed_source(conn, "direct:u", "u")
        now = now_iso()
        ch = content_hash("t", "s", vault)
        mem = Memory(
            memory_id=f"mem-{vault}",
            user_id="u",
            source_id="direct:u",
            vault_path=vault,
            title="t",
            summary="s",
            content_hash=ch,
            state="active",
            created_at=now,
            updated_at=now,
        )
        insert_memory(conn, mem)
        return mem

    def _insert_raw_claim(
        self, conn: sqlite3.Connection, subj: str
    ) -> Claim:
        _seed_source(conn, "direct:u", "u")
        now = now_iso()
        ch = content_hash(subj, "y", "z", "direct:u", "u")
        cla = Claim(
            claim_id=f"cla-{subj}",
            user_id="u",
            subject=subj,
            predicate="y",
            object="z",
            source_id="direct:u",
            content_hash=ch,
            confidence=None,
            state="auto",
            created_at=now,
            updated_at=now,
        )
        insert_claim(conn, cla)
        return cla

    def test_backfill_synthesizes_create_events(
        self, conn: sqlite3.Connection
    ) -> None:
        self._insert_raw_memory(conn, "v1.md")
        self._insert_raw_memory(conn, "v2.md")
        self._insert_raw_claim(conn, "a")
        summary = backfill_creation_events(conn)
        assert isinstance(summary, BackfillSummary)
        assert summary.memory_creations_added == 2
        assert summary.claim_creations_added == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'memory.created'"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'claim.created'"
            ).fetchone()[0]
            == 1
        )

    def test_backfill_is_idempotent(self, conn: sqlite3.Connection) -> None:
        self._insert_raw_memory(conn, "v1.md")
        backfill_creation_events(conn)
        second = backfill_creation_events(conn)
        assert second.memory_creations_added == 0
        assert second.claim_creations_added == 0

    def test_backfill_plus_replay_reproduces_rows(
        self, conn: sqlite3.Connection, tmp_path: pathlib.Path
    ) -> None:
        self._insert_raw_memory(conn, "v1.md")
        self._insert_raw_memory(conn, "v2.md")
        self._insert_raw_claim(conn, "a")
        backfill_creation_events(conn)
        src_mems = _rows(conn, "SELECT * FROM memories ORDER BY memory_id")
        src_claims = _rows(conn, "SELECT * FROM claims ORDER BY claim_id")

        fresh = _fresh_conn(tmp_path, "bf.db")
        _seed_source(fresh, "direct:u", "u")
        replay_events(conn, into_conn=fresh)

        assert _rows(fresh, "SELECT * FROM memories ORDER BY memory_id") == src_mems
        assert _rows(fresh, "SELECT * FROM claims ORDER BY claim_id") == src_claims
        fresh.close()

    def test_backfill_skips_rows_that_already_have_created(
        self, conn: sqlite3.Connection
    ) -> None:
        # An ingest_memory already emits memory.created, so backfill finds
        # nothing to add.
        ingest_memory(conn, user_id="u", title="t", summary="s", vault_path="v.md")
        summary = backfill_creation_events(conn)
        assert summary.memory_creations_added == 0
