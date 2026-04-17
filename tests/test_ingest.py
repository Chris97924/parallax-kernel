"""Tests for parallax.ingest.

Contract:
    * ``ingest_memory`` / ``ingest_claim`` compute content_hash and UPSERT.
    * Missing ``source_id`` triggers auto-upsert of the synthetic source
      ``direct:<user_id>``.
    * Duplicate content is absorbed: the existing row's id is returned and
      the row count does not grow.
"""

from __future__ import annotations

import sqlite3

from parallax.hashing import content_hash
from parallax.ingest import ingest_claim, ingest_memory, synthetic_direct_source_id
from parallax.sqlite_store import query


class TestSyntheticSource:
    def test_source_id_format(self) -> None:
        assert synthetic_direct_source_id("chris") == "direct:chris"

    def test_auto_creates_source_on_first_direct_memory(
        self, conn: sqlite3.Connection
    ) -> None:
        assert query(conn, "SELECT COUNT(*) AS n FROM sources", ())[0]["n"] == 0
        ingest_memory(conn, user_id="chris", title="t", summary="s", vault_path="v.md")
        rows = query(conn, "SELECT * FROM sources WHERE source_id = ?", ("direct:chris",))
        assert len(rows) == 1
        assert rows[0]["kind"] == "chat"
        assert rows[0]["uri"] == "parallax://direct/chris"
        assert rows[0]["user_id"] == "chris"

    def test_synthetic_source_idempotent_across_calls(
        self, conn: sqlite3.Connection
    ) -> None:
        ingest_memory(conn, user_id="chris", title="a", summary="b", vault_path="p1.md")
        ingest_memory(conn, user_id="chris", title="c", summary="d", vault_path="p2.md")
        rows = query(conn, "SELECT COUNT(*) AS n FROM sources WHERE source_id = ?", ("direct:chris",))
        assert rows[0]["n"] == 1


class TestIngestMemoryUpsert:
    def test_returns_non_empty_id(self, conn: sqlite3.Connection) -> None:
        mid = ingest_memory(conn, user_id="chris", title="t", summary="s", vault_path="v.md")
        assert isinstance(mid, str) and len(mid) > 0

    def test_duplicate_content_is_absorbed(self, conn: sqlite3.Connection) -> None:
        mid1 = ingest_memory(conn, user_id="chris", title="t", summary="s", vault_path="v.md")
        mid2 = ingest_memory(conn, user_id="chris", title="t", summary="s", vault_path="v.md")
        assert mid1 == mid2
        rows = query(conn, "SELECT COUNT(*) AS n FROM memories", ())
        assert rows[0]["n"] == 1

    def test_different_content_creates_new_row(self, conn: sqlite3.Connection) -> None:
        ingest_memory(conn, user_id="chris", title="t1", summary="s", vault_path="v.md")
        ingest_memory(conn, user_id="chris", title="t2", summary="s", vault_path="v.md")
        rows = query(conn, "SELECT COUNT(*) AS n FROM memories", ())
        assert rows[0]["n"] == 2

    def test_returns_persisted_id_even_when_row_pre_exists(
        self, conn: sqlite3.Connection
    ) -> None:
        # phantom-ID race regression: identical content must return the
        # persisted id, not a freshly minted ULID dropped by INSERT OR IGNORE.
        m1 = ingest_memory(
            conn, user_id="chris", title="t", summary="s", vault_path="v.md"
        )
        m2 = ingest_memory(
            conn, user_id="chris", title="t", summary="s", vault_path="v.md"
        )
        assert m1 == m2
        row = query(conn, "SELECT memory_id FROM memories WHERE memory_id = ?", (m2,))
        assert len(row) == 1  # persisted, not phantom

    def test_across_users_independent(self, conn: sqlite3.Connection) -> None:
        # Same content under two users -> two rows (user_id participates in UNIQUE).
        ingest_memory(conn, user_id="chris", title="t", summary="s", vault_path="v.md")
        ingest_memory(conn, user_id="alice", title="t", summary="s", vault_path="v.md")
        rows = query(conn, "SELECT COUNT(*) AS n FROM memories", ())
        assert rows[0]["n"] == 2


class TestIngestClaimUpsert:
    def test_returns_non_empty_id(self, conn: sqlite3.Connection) -> None:
        cid = ingest_claim(conn, user_id="chris", subject="chris", predicate="likes", object_="coffee")
        assert isinstance(cid, str) and len(cid) > 0

    def test_duplicate_claim_absorbed(self, conn: sqlite3.Connection) -> None:
        c1 = ingest_claim(conn, user_id="chris", subject="chris", predicate="likes", object_="coffee")
        c2 = ingest_claim(conn, user_id="chris", subject="chris", predicate="likes", object_="coffee")
        assert c1 == c2
        rows = query(conn, "SELECT COUNT(*) AS n FROM claims", ())
        assert rows[0]["n"] == 1

    def test_content_hash_matches_schema_formula(self, conn: sqlite3.Connection) -> None:
        cid = ingest_claim(conn, user_id="chris", subject="chris", predicate="likes", object_="coffee")
        expected = content_hash("chris", "likes", "coffee", "direct:chris")
        row = query(conn, "SELECT content_hash FROM claims WHERE claim_id = ?", (cid,))[0]
        assert row["content_hash"] == expected

    def test_returns_persisted_id_even_when_row_pre_exists(
        self, conn: sqlite3.Connection
    ) -> None:
        # Simulates the phantom-ID race: a row with the same content_hash is
        # already persisted under a known id. The second ingest call must
        # return THAT id, not a freshly minted ULID that INSERT OR IGNORE
        # would have silently dropped.
        c1 = ingest_claim(
            conn, user_id="chris", subject="chris", predicate="likes", object_="coffee"
        )
        c2 = ingest_claim(
            conn, user_id="chris", subject="chris", predicate="likes", object_="coffee"
        )
        assert c1 == c2
        row = query(conn, "SELECT claim_id FROM claims WHERE claim_id = ?", (c2,))
        assert len(row) == 1  # persisted, not phantom

    def test_explicit_source_id_skips_synthetic(self, conn: sqlite3.Connection) -> None:
        # Pre-insert a real source
        conn.execute(
            """INSERT INTO sources(source_id, uri, kind, content_hash, user_id,
                                    ingested_at, state)
               VALUES (?, ?, ?, ?, ?, datetime('now'), 'ingested')""",
            ("src-xyz", "file://foo.md", "file", "deadbeef", "chris"),
        )
        conn.commit()
        ingest_claim(
            conn,
            user_id="chris",
            subject="chris",
            predicate="likes",
            object_="tea",
            source_id="src-xyz",
        )
        # The synthetic source should NOT be created.
        rows = query(conn, "SELECT COUNT(*) AS n FROM sources WHERE source_id = ?", ("direct:chris",))
        assert rows[0]["n"] == 0
