"""Integration tests for parallax.extract.ingest.extract_and_ingest."""

from __future__ import annotations

import sqlite3

from parallax.extract import RawClaim, extract_and_ingest
from parallax.extract.ingest import claim_predicate
from parallax.extract.providers.mock import MockProvider
from parallax.hashing import content_hash
from parallax.retrieve import claim_by_content_hash
from parallax.sqlite_store import query


def _raw(entity: str, text: str, polarity: int = 1, ctype: str = "feature") -> RawClaim:
    return RawClaim(
        entity=entity,
        claim_text=text,
        polarity=polarity,
        confidence=0.9,
        claim_type=ctype,
        evidence="",
    )


def test_empty_text_returns_empty(conn: sqlite3.Connection) -> None:
    p = MockProvider(claims=[_raw("x", "y")])
    assert extract_and_ingest(conn, "", provider=p, user_id="chris") == []
    # provider not called
    assert p.calls == []


def test_empty_provider_output_returns_empty(conn: sqlite3.Connection) -> None:
    assert (
        extract_and_ingest(conn, "some text", provider=MockProvider(), user_id="chris")
        == []
    )


def test_three_claims_persisted_and_retrievable(conn: sqlite3.Connection) -> None:
    raws = [
        _raw("bitcoin", "It is volatile", polarity=-1, ctype="risk"),
        _raw("bitcoin", "It is decentralized", polarity=1, ctype="feature"),
        _raw("remote-work", "Commute disappears", polarity=1, ctype="feature"),
    ]
    p = MockProvider(claims=raws)
    ids = extract_and_ingest(conn, "text", provider=p, user_id="chris")
    assert len(ids) == 3
    # retrieval via content_hash matches
    for raw, cid in zip(raws, ids, strict=True):
        ch = content_hash(
            raw.entity,
            claim_predicate(raw),
            raw.claim_text,
            "direct:chris",
        )
        row = claim_by_content_hash(conn, ch)
        assert row is not None
        assert row["claim_id"] == cid


def test_dedup_second_run_returns_same_ids(conn: sqlite3.Connection) -> None:
    raws = [_raw("x", "hello world")]
    p = MockProvider(claims=raws)
    ids1 = extract_and_ingest(conn, "t", provider=p, user_id="chris")
    ids2 = extract_and_ingest(conn, "t", provider=p, user_id="chris")
    assert ids1 == ids2
    rows = query(conn, "SELECT COUNT(*) AS n FROM claims", ())
    assert rows[0]["n"] == 1


def test_polarity_encoded_in_predicate(conn: sqlite3.Connection) -> None:
    raws = [
        _raw("bitcoin", "x", polarity=1, ctype="feature"),
        _raw("bitcoin", "x", polarity=-1, ctype="feature"),
    ]
    ids = extract_and_ingest(
        conn, "t", provider=MockProvider(claims=raws), user_id="chris"
    )
    assert len(ids) == 2  # opposite polarity => different hash => two rows
    rows = query(
        conn, "SELECT predicate FROM claims WHERE subject = ? ORDER BY predicate", ("bitcoin",)
    )
    preds = {r["predicate"] for r in rows}
    assert preds == {"feature/+1", "feature/-1"}


def test_source_id_propagated(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO sources(source_id, uri, kind, content_hash, user_id,
                               ingested_at, state)
           VALUES (?, ?, ?, ?, ?, datetime('now'), 'ingested')""",
        ("custom-src", "file://a", "file", "deadbeef01", "chris"),
    )
    conn.commit()
    ids = extract_and_ingest(
        conn,
        "t",
        provider=MockProvider(claims=[_raw("x", "y")]),
        user_id="chris",
        source_id="custom-src",
    )
    assert len(ids) == 1
    rows = query(conn, "SELECT source_id FROM claims WHERE claim_id = ?", (ids[0],))
    assert rows[0]["source_id"] == "custom-src"
    # synthetic source should not have been created
    rows2 = query(
        conn, "SELECT COUNT(*) AS n FROM sources WHERE source_id = ?", ("direct:chris",)
    )
    assert rows2[0]["n"] == 0
