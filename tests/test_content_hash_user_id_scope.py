"""BUG 3 / ADR-005 regression: claim content_hash is scoped to user_id.

Without ``user_id`` in the hash, two users who ingest the same triple from
the same (shared) source collapse onto a single claim row under
``UNIQUE(content_hash, source_id)``. One user's knowledge wins; the other
silently dedupes away. v0.5.0-pre1 makes the hash include ``user_id`` so
per-user claims stay distinct.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.hashing import content_hash
from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect, now_iso, query


def _seed_source(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO sources(source_id, uri, kind, content_hash,
                                         user_id, ingested_at, state)
           VALUES (?, ?, ?, ?, ?, ?, 'ingested')""",
        (source_id, f"file://{source_id}.md", "file", f"hash-{source_id}",
         "shared-owner", now_iso()),
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "ch.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


class TestCrossUserSameSourceSameTriple:
    def test_two_users_same_triple_and_source_produce_two_rows(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_source(conn, "shared-src")
        c_alice = ingest_claim(
            conn,
            user_id="alice",
            subject="chris",
            predicate="likes",
            object_="coffee",
            source_id="shared-src",
        )
        c_bob = ingest_claim(
            conn,
            user_id="bob",
            subject="chris",
            predicate="likes",
            object_="coffee",
            source_id="shared-src",
        )
        assert c_alice != c_bob
        rows = query(conn, "SELECT COUNT(*) AS n FROM claims", ())
        assert rows[0]["n"] == 2

    def test_content_hash_formula_includes_user_id(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_source(conn, "src-u")
        cid = ingest_claim(
            conn,
            user_id="alice",
            subject="x",
            predicate="y",
            object_="z",
            source_id="src-u",
        )
        expected = content_hash("x", "y", "z", "src-u", "alice")
        row = query(
            conn,
            "SELECT content_hash FROM claims WHERE claim_id = ?",
            (cid,),
        )[0]
        assert row["content_hash"] == expected


class TestMigrationRehash:
    def test_m0007_rehashes_existing_claims_with_new_formula(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Seed a DB at migration version 6 (old-formula claims), then run
        migrate_to_latest and verify every claim's content_hash matches the
        new 5-part formula ``sha256(subject||predicate||object||source_id||user_id)``.
        """
        from parallax.migrations import (
            MIGRATIONS,
            _manual_tx,
            ensure_schema_migrations_table,
        )

        db = tmp_path / "mig.db"
        c = connect(db)
        ensure_schema_migrations_table(c)
        for mig in sorted(MIGRATIONS, key=lambda m: m.version):
            if mig.version > 6:
                continue
            with _manual_tx(c):
                mig.up(c)
                c.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (mig.version, mig.name, now_iso()),
                )

        # Seed source + two rows using the OLD 4-part hash formula.
        _seed_source(c, "src-a")
        old_hash_1 = content_hash("s1", "p1", "o1", "src-a")
        old_hash_2 = content_hash("s2", "p2", "o2", "src-a")
        ts = now_iso()
        for cid, user, subj, pred, obj, old_h in [
            ("c1", "alice", "s1", "p1", "o1", old_hash_1),
            ("c2", "bob", "s2", "p2", "o2", old_hash_2),
        ]:
            c.execute(
                """INSERT INTO claims(claim_id, user_id, subject, predicate,
                                      object, source_id, content_hash,
                                      confidence, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'auto', ?, ?)""",
                (cid, user, subj, pred, obj, "src-a", old_h, ts, ts),
            )
        c.commit()

        # Apply m0007 by running migrate_to_latest.
        migrate_to_latest(c)

        # Every row now carries the new 5-part hash.
        expected_1 = content_hash("s1", "p1", "o1", "src-a", "alice")
        expected_2 = content_hash("s2", "p2", "o2", "src-a", "bob")
        row = query(c, "SELECT content_hash FROM claims WHERE claim_id = ?", ("c1",))[0]
        assert row["content_hash"] == expected_1
        row = query(c, "SELECT content_hash FROM claims WHERE claim_id = ?", ("c2",))[0]
        assert row["content_hash"] == expected_2

        # New UNIQUE index has 3 columns.
        idx = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='uniq_claims_content'"
        ).fetchone()
        assert idx is not None
        assert "user_id" in idx[0]
        c.close()

    def test_m0007_down_restores_four_part_hash(
        self, tmp_path: pathlib.Path
    ) -> None:
        from parallax.migrations import migrate_down_to

        db = tmp_path / "migdown.db"
        c = connect(db)
        migrate_to_latest(c)

        _seed_source(c, "src-d")
        ingest_claim(
            c,
            user_id="alice",
            subject="s",
            predicate="p",
            object_="o",
            source_id="src-d",
        )
        # New-formula hash on disk.
        after_up = content_hash("s", "p", "o", "src-d", "alice")
        row = query(c, "SELECT content_hash FROM claims", ())[0]
        assert row["content_hash"] == after_up

        migrate_down_to(c, 6)
        after_down = content_hash("s", "p", "o", "src-d")
        row = query(c, "SELECT content_hash FROM claims", ())[0]
        assert row["content_hash"] == after_down
        c.close()
