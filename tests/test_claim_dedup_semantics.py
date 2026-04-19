"""Regression tests for ADR-004 — claim dedup includes source_id.

The content_hash formula `sha256(normalize(subject||predicate||object||source_id))`
IS the dedup key. Same triple under different source_ids = two distinct rows.
"""

from __future__ import annotations

import sqlite3

from parallax.hashing import content_hash
from parallax.ingest import ingest_claim
from parallax.sqlite_store import query


def _seed_source(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        """INSERT INTO sources(source_id, uri, kind, content_hash, user_id,
                               ingested_at, state)
           VALUES (?, ?, ?, ?, ?, datetime('now'), 'ingested')""",
        (source_id, f"file://{source_id}.md", "file", f"hash-{source_id}", "chris"),
    )
    conn.commit()


class TestClaimDedupSemantics:
    def test_same_triple_different_source_produces_two_rows(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_source(conn, "src-a")
        _seed_source(conn, "src-b")
        c1 = ingest_claim(
            conn,
            user_id="chris",
            subject="chris",
            predicate="likes",
            object_="coffee",
            source_id="src-a",
        )
        c2 = ingest_claim(
            conn,
            user_id="chris",
            subject="chris",
            predicate="likes",
            object_="coffee",
            source_id="src-b",
        )
        assert c1 != c2
        rows = query(conn, "SELECT COUNT(*) AS n FROM claims", ())
        assert rows[0]["n"] == 2

    def test_same_triple_same_source_is_idempotent(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_source(conn, "src-a")
        c1 = ingest_claim(
            conn,
            user_id="chris",
            subject="chris",
            predicate="likes",
            object_="coffee",
            source_id="src-a",
        )
        c2 = ingest_claim(
            conn,
            user_id="chris",
            subject="chris",
            predicate="likes",
            object_="coffee",
            source_id="src-a",
        )
        assert c1 == c2
        rows = query(conn, "SELECT COUNT(*) AS n FROM claims", ())
        assert rows[0]["n"] == 1

    def test_content_hash_formula_matches_schema(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_source(conn, "src-xyz")
        cid = ingest_claim(
            conn,
            user_id="chris",
            subject="chris",
            predicate="likes",
            object_="coffee",
            source_id="src-xyz",
        )
        expected = content_hash("chris", "likes", "coffee", "src-xyz")
        row = query(
            conn,
            "SELECT content_hash FROM claims WHERE claim_id = ?",
            (cid,),
        )[0]
        assert row["content_hash"] == expected

    def test_synthetic_and_explicit_source_are_distinct(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_source(conn, "real-src")
        c1 = ingest_claim(
            conn, user_id="chris", subject="x", predicate="y", object_="z"
        )
        c2 = ingest_claim(
            conn,
            user_id="chris",
            subject="x",
            predicate="y",
            object_="z",
            source_id="real-src",
        )
        assert c1 != c2
        rows = query(conn, "SELECT COUNT(*) AS n FROM claims", ())
        assert rows[0]["n"] == 2
