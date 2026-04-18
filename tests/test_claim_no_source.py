"""P0-03: claim with source_id=None regression test.

ingest_claim with source_id=None must:
  * succeed via synthetic 'direct:<user_id>' source
  * dedup on re-call (same claim_id)
  * auto-create the synthetic source row
"""

from __future__ import annotations

from parallax.ingest import ingest_claim
from parallax.sqlite_store import query


def test_claim_none_source_uses_synthetic_direct(conn) -> None:
    cid1 = ingest_claim(
        conn, user_id="u1", subject="x", predicate="y", object_="z", source_id=None
    )
    assert cid1

    cid2 = ingest_claim(
        conn, user_id="u1", subject="x", predicate="y", object_="z", source_id=None
    )
    assert cid2 == cid1, "UNIQUE(content_hash, source_id) dedup must fire on re-ingest"

    claims = query(conn, "SELECT source_id FROM claims WHERE claim_id = ?", (cid1,))
    assert claims[0]["source_id"] == "direct:u1"

    sources = query(conn, "SELECT source_id FROM sources WHERE source_id = ?", ("direct:u1",))
    assert len(sources) == 1
