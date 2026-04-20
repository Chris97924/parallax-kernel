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


def test_recency_top3_pinned_to_front():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    _seed_claims(conn, n=30)

    evidence = fallback_retrieve(conn, "u1", "coffee tennis", k_max=10)
    assert len(evidence.hits) >= 3
    front_dates = [h["created_at"] for h in evidence.hits[:3]]
    all_dates = [h["created_at"] for h in evidence.hits]
    # The three at the front are the three most recent of the selected set.
    assert sorted(front_dates, reverse=True) == sorted(all_dates, reverse=True)[:3]
