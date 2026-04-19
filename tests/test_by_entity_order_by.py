"""BUG 2 regression: by_entity must return the highest-confidence claims when
matching rows exceed the limit. Without ``ORDER BY`` on the SQL LIMIT, SQLite
returns rows in heap (rowid) order, so the post-fetch ``hits.sort()`` can only
sort an already-truncated prefix — truly high-confidence rows at rowid > limit
disappear."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.retrieve import by_entity
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "ent.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


class TestByEntityOrderBy:
    def test_by_entity_returns_highest_confidence_when_matches_exceed_limit(
        self, conn: sqlite3.Connection
    ) -> None:
        # Ingest 25 matching claims with confidence increasing from 0.01..0.25.
        # Insertion order (and therefore rowid) is ASCENDING, so if the SQL
        # selects rowid-first-20, the top-confidence rows (rowid 21..25) are
        # dropped — the very ones that SHOULD surface.
        for i in range(25):
            ingest_claim(
                conn,
                user_id="u",
                subject="Python",
                predicate="pred",
                object_=f"o{i:02d}",
                confidence=round((i + 1) * 0.01, 4),
            )
        hits = by_entity(conn, user_id="u", subject="Python", limit=20)
        claim_hits = [h for h in hits if h.entity_kind == "claim"]
        assert len(claim_hits) == 20
        confidences = [h.full["confidence"] for h in claim_hits]
        # Top 20 must be confidences 0.06..0.25, not 0.01..0.20.
        assert min(confidences) >= 0.06, (
            f"expected top-20-by-confidence {{0.06..0.25}}, got "
            f"min={min(confidences)} (rowid-first-20 regression)"
        )
        assert max(confidences) == pytest.approx(0.25)

    def test_by_entity_tiebreak_is_deterministic(
        self, conn: sqlite3.Connection
    ) -> None:
        # Identical confidence for every claim; the SQL ORDER BY guarantees
        # a stable shape so repeated calls return the same ordering.
        for i in range(10):
            ingest_claim(
                conn,
                user_id="u",
                subject="Python",
                predicate="pred",
                object_=f"o{i:02d}",
                confidence=0.5,
            )
        h1 = by_entity(conn, user_id="u", subject="Python", limit=5)
        h2 = by_entity(conn, user_id="u", subject="Python", limit=5)
        ids1 = [h.entity_id for h in h1 if h.entity_kind == "claim"]
        ids2 = [h.entity_id for h in h2 if h.entity_kind == "claim"]
        assert ids1 == ids2
        assert len(ids1) == 5
        # Without ORDER BY the set would be rowid-first-5; with the new
        # ORDER BY (confidence DESC, updated_at DESC, claim_id ASC) ties on
        # confidence fall through to updated_at DESC → the LATEST-inserted
        # rows win, which is the DESC insertion order.
        all_ids_desc = [f"o{i:02d}" for i in range(9, -1, -1)][:5]
        returned_objects = [h.full["object"] for h in h1 if h.entity_kind == "claim"]
        assert returned_objects == all_ids_desc
