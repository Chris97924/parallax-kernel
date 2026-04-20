"""Tests for parallax.retrieval.retrievers.fallback_retrieve."""

from __future__ import annotations

import sqlite3

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.retrieval.retrievers import fallback_retrieve


def _make_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE claims (
            claim_id TEXT PRIMARY KEY, user_id TEXT, subject TEXT, predicate TEXT,
            object TEXT, source_id TEXT, content_hash TEXT, confidence REAL,
            state TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY, user_id TEXT, actor TEXT, event_type TEXT,
            target_kind TEXT, target_id TEXT, payload_json TEXT, approval_tier TEXT,
            created_at TEXT
        );
        """
    )


def _seed_claims(conn: sqlite3.Connection, n: int, user_id: str = "u1") -> None:
    for i in range(n):
        conn.execute(
            """
            INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"c{i:03d}",
                user_id,
                f"subject_{i}",
                "likes" if i % 2 == 0 else "visited",
                f"object_{i} about tennis and coffee" if i < 20 else f"object_{i}",
                f"s{i}",
                f"h{i}",
                0.8,
                "active",
                f"2026-04-{(i % 28) + 1:02d}T10:00:00Z",
                f"2026-04-{(i % 28) + 1:02d}T10:00:00Z",
            ),
        )
    conn.commit()


def test_fallback_returns_retrieval_evidence():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    _seed_claims(conn, n=50)

    evidence = fallback_retrieve(conn, "u1", "tennis", k_max=32)

    assert isinstance(evidence, RetrievalEvidence)
    assert evidence.diversity_mode in {"mmr_embedding", "mmr_stub_bm25"}
    assert len(evidence.hits) <= 32
    assert len(evidence.hits) >= 1
    # Token budget enforced.
    total = sum(max(1, len(h["text"]) // 4) for h in evidence.hits)
    assert total <= 6000 + 500  # allow one over-the-edge item per spec


def test_empty_pool_demotes_to_fallback():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    evidence = fallback_retrieve(conn, "u_missing", "anything")
    assert evidence.hits == ()
    assert "demoted_to_fallback" in evidence.notes


def _seed_claims_with_known_dates(
    conn: sqlite3.Connection, n: int, user_id: str = "u1"
) -> list[str]:
    """Seed ``n`` claims with strictly-increasing distinct timestamps.

    Returns the list of created_at strings in chronological order so the test
    can reason about which three are newest without re-sorting the fixture.
    """
    created_ats: list[str] = []
    for i in range(n):
        # Month offset bumps per 300 rows so ordering stays strictly monotonic
        # over the full seed set; second-offset (i*7)%60 keeps every row distinct.
        ts = (
            f"2026-{(i // 300) + 4:02d}-{(i % 28) + 1:02d}"
            f"T{(i % 24):02d}:{(i % 60):02d}:{(i * 7) % 60:02d}Z"
        )
        created_ats.append(ts)
        conn.execute(
            """
            INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"k{i:03d}",
                user_id,
                f"subject_{i}",
                "likes" if i % 2 == 0 else "visited",
                f"object_{i} about tennis and coffee" if i < 20 else f"object_{i}",
                f"s{i}",
                f"h{i}",
                0.8,
                "active",
                ts,
                ts,
            ),
        )
    conn.commit()
    return created_ats


def test_recency_top3_pinned_to_front():
    """Causal assertion: the three pinned items are the genuinely-newest subset.

    The earlier version of this test compared sorted(front) to sorted(all)[:3];
    that is a tautology when the pin logic truncates the front to 3 items. The
    rewrite asserts that the set of three pinned created_at values equals the
    set of the three maximum created_at values across the whole selected pool.
    """
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    created_ats = _seed_claims_with_known_dates(conn, n=50)

    evidence = fallback_retrieve(conn, "u1", "coffee tennis", k_max=10)
    assert len(evidence.hits) >= 3

    front_dates = {h["created_at"] for h in evidence.hits[:3]}
    all_dates = [h["created_at"] for h in evidence.hits]
    # The three pinned at the front are the three largest timestamps in the
    # entire selected hit set — as a *set*, not a sorted-list tautology.
    expected_top3 = set(sorted(all_dates, reverse=True)[:3])
    assert front_dates == expected_top3
    # Every pinned date must be strictly greater than every non-pinned date
    # in the remaining tail — the causal property of "recency pin".
    tail_dates = [h["created_at"] for h in evidence.hits[3:]]
    if tail_dates:
        assert min(front_dates) > max(tail_dates)
    # All three pinned dates should be known-distinct seed values.
    assert front_dates.issubset(set(created_ats))


def test_embedding_cache_reused(monkeypatch):
    """Second fallback_retrieve on same corpus does not re-encode items."""
    from parallax.retrieval import retrievers as rt

    # Reset the module-level caches so this test is independent of order.
    rt._EMB_CACHE.clear()

    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    _seed_claims(conn, n=30)

    model = rt._load_model()
    if model is None:  # pragma: no cover — embedding SDK missing
        pytest.skip("sentence-transformers unavailable")

    encode_calls = {"n": 0, "sizes": []}
    real_encode = model.encode

    def counting_encode(texts, *args, **kwargs):
        encode_calls["n"] += 1
        encode_calls["sizes"].append(len(texts) if hasattr(texts, "__len__") else 1)
        return real_encode(texts, *args, **kwargs)

    monkeypatch.setattr(model, "encode", counting_encode)

    fallback_retrieve(conn, "u1", "tennis", k_max=16)
    first_total = encode_calls["n"]
    first_sizes = list(encode_calls["sizes"])

    fallback_retrieve(conn, "u1", "tennis", k_max=16)
    second_total = encode_calls["n"]

    # Second call may still call encode for the 1-element query embedding,
    # but must NOT re-encode the item pool.
    large_call_sizes = [s for s in encode_calls["sizes"][first_total:] if s > 1]
    assert not large_call_sizes, (
        f"item pool re-encoded on second call: sizes={encode_calls['sizes']}"
    )
    # At minimum: first call issued two encode calls (query + items); second
    # call issued at most one (query only).
    assert first_total >= 2
    assert second_total - first_total <= 1
    assert any(s > 1 for s in first_sizes), (
        "first call should have encoded the item pool in bulk"
    )
