"""Tests for parallax.index.rebuild_index — minimal index_state replay."""

from __future__ import annotations

import sqlite3

from parallax.events import record_memory_reaffirmed
from parallax.index import rebuild_index
from parallax.ingest import ingest_claim, ingest_memory


class TestRebuildIndex:
    def test_fresh_db_returns_version_one_zero_docs(
        self, conn: sqlite3.Connection
    ) -> None:
        out = rebuild_index(conn, "chroma")
        assert out["index_name"] == "chroma"
        assert out["version"] == 1
        assert out["doc_count"] == 0
        assert out["state"] == "ready"
        assert out["error_text"] is None
        assert out["source_watermark"] is None

    def test_second_rebuild_bumps_version(self, conn: sqlite3.Connection) -> None:
        rebuild_index(conn, "chroma")
        out = rebuild_index(conn, "chroma")
        assert out["version"] == 2

    def test_versions_are_per_index_name(self, conn: sqlite3.Connection) -> None:
        a = rebuild_index(conn, "chroma")
        b = rebuild_index(conn, "memvid")
        # Independent counters per index_name
        assert a["version"] == 1
        assert b["version"] == 1

    def test_doc_count_reflects_active_memories_and_claims(
        self, conn: sqlite3.Connection
    ) -> None:
        ingest_memory(conn, user_id="u", title="t1", summary="s1", vault_path="v1.md")
        ingest_memory(conn, user_id="u", title="t2", summary="s2", vault_path="v2.md")
        ingest_claim(conn, user_id="u", subject="x", predicate="y", object_="z")
        out = rebuild_index(conn, "chroma")
        assert out["doc_count"] == 3

    def test_source_watermark_tracks_last_event(
        self, conn: sqlite3.Connection
    ) -> None:
        mid = ingest_memory(
            conn, user_id="u", title="t", summary="s", vault_path="v.md"
        )
        eid = record_memory_reaffirmed(conn, user_id="u", memory_id=mid)
        out = rebuild_index(conn, "chroma")
        assert out["source_watermark"] == eid

    def test_history_preserved(self, conn: sqlite3.Connection) -> None:
        rebuild_index(conn, "chroma")
        rebuild_index(conn, "chroma")
        rows = conn.execute(
            "SELECT version FROM index_state WHERE index_name = 'chroma' ORDER BY version"
        ).fetchall()
        assert [r[0] for r in rows] == [1, 2]
