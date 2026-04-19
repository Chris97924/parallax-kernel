"""BUG 2 regression: by_bug_fix claim SELECT must apply ORDER BY.

Same mechanism as tests/test_by_entity_order_by.py — SQLite's ``LIMIT ?``
without an explicit ``ORDER BY`` returns heap-order rows, and the post-fetch
``hits.sort()`` can only reorder an already-truncated prefix.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.hashing import content_hash
from parallax.migrations import migrate_to_latest
from parallax.retrieve import by_bug_fix
from parallax.sqlite_store import connect, now_iso

# Direct claim insert bypassing ingest_claim so we don't emit a
# ``claim.created`` event that would also match the by_bug_fix event
# LIKE scan and dominate the final score sort — the tests here isolate
# claim-side ORDER BY semantics.


def _seed_direct_source(conn: sqlite3.Connection, user_id: str) -> str:
    source_id = f"src-bf-{user_id}"
    conn.execute(
        """INSERT OR IGNORE INTO sources(source_id, uri, kind, content_hash,
                                         user_id, ingested_at, state)
           VALUES (?, ?, 'file', ?, ?, ?, 'ingested')""",
        (source_id, f"file://{source_id}.md", f"hash-{source_id}", user_id, now_iso()),
    )
    conn.commit()
    return source_id


def _raw_insert_claim(
    conn: sqlite3.Connection,
    *,
    claim_id: str,
    user_id: str,
    subject: str,
    predicate: str,
    object_: str,
    source_id: str,
    confidence: float | None,
) -> None:
    ch = content_hash(subject, predicate, object_, source_id, user_id)
    ts = now_iso()
    conn.execute(
        """INSERT INTO claims(claim_id, user_id, subject, predicate, object,
                              source_id, content_hash, confidence, state,
                              created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?, ?)""",
        (claim_id, user_id, subject, predicate, object_, source_id, ch,
         confidence, ts, ts),
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "bf.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


class TestByBugFixOrderBy:
    def test_by_bug_fix_claims_returns_highest_confidence_when_exceeding_limit(
        self, conn: sqlite3.Connection
    ) -> None:
        source_id = _seed_direct_source(conn, "u")
        # 25 matching claims via subject containing "bug"; confidence varies
        # with claim_id order ascending so that claim_id ASC and confidence
        # ASC line up. If SQL returns rowid-first-20 (no ORDER BY), the
        # top-confidence tail (rowid 21..25) is lost.
        for i in range(25):
            _raw_insert_claim(
                conn,
                claim_id=f"c{i:02d}",
                user_id="u",
                subject=f"bug report {i:02d}",
                predicate="pred",
                object_=f"o{i:02d}",
                source_id=source_id,
                confidence=round((i + 1) * 0.01, 4),
            )
        hits = by_bug_fix(conn, user_id="u", limit=20)
        claim_hits = [h for h in hits if h.entity_kind == "claim"]
        assert len(claim_hits) == 20
        confidences = [h.full["confidence"] for h in claim_hits]
        assert min(confidences) >= 0.06, (
            f"expected top-20-by-confidence {{0.06..0.25}}, got "
            f"min={min(confidences)} (rowid-first-20 regression)"
        )
        assert max(confidences) == pytest.approx(0.25)

    def test_by_bug_fix_tiebreak_is_deterministic(
        self, conn: sqlite3.Connection
    ) -> None:
        source_id = _seed_direct_source(conn, "u")
        for i in range(10):
            _raw_insert_claim(
                conn,
                claim_id=f"t{i:02d}",
                user_id="u",
                subject=f"bug row {i:02d}",
                predicate="pred",
                object_=f"o{i:02d}",
                source_id=source_id,
                confidence=0.5,
            )
        h1 = by_bug_fix(conn, user_id="u", limit=5)
        h2 = by_bug_fix(conn, user_id="u", limit=5)
        ids1 = [h.entity_id for h in h1 if h.entity_kind == "claim"]
        ids2 = [h.entity_id for h in h2 if h.entity_kind == "claim"]
        assert ids1 == ids2
