"""Tests for parallax.retrieve.

Contract:
    * Lookup functions return list[dict] / Optional[dict] -- never sqlite3.Row.
    * user isolation, state filter optional, subject exact-match.
    * content_hash lookup returns None when absent.
"""

from __future__ import annotations

import sqlite3

import pytest

from parallax.hashing import content_hash
from parallax.ingest import ingest_claim, ingest_memory
from parallax.retrieve import (
    claim_by_content_hash,
    claims_by_subject,
    claims_by_user,
    memories_by_user,
    memory_by_content_hash,
)


@pytest.fixture()
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    # Chris memories
    ingest_memory(conn, user_id="chris", title="a", summary="s1", vault_path="a.md")
    ingest_memory(conn, user_id="chris", title="b", summary="s2", vault_path="b.md")
    # Alice memories
    ingest_memory(conn, user_id="alice", title="a1", summary="s", vault_path="x.md")
    # Chris claims
    ingest_claim(conn, user_id="chris", subject="chris", predicate="likes", object_="coffee")
    ingest_claim(conn, user_id="chris", subject="chris", predicate="likes", object_="tea")
    ingest_claim(conn, user_id="chris", subject="obsidian", predicate="is", object_="vault")
    # Archive one memory for state-filter coverage
    conn.execute("UPDATE memories SET state = 'archived' WHERE vault_path = 'b.md'")
    conn.commit()
    return conn


class TestMemoriesByUser:
    def test_returns_dicts_not_rows(self, seeded: sqlite3.Connection) -> None:
        rows = memories_by_user(seeded, "chris")
        assert all(isinstance(r, dict) for r in rows)

    def test_user_isolation(self, seeded: sqlite3.Connection) -> None:
        chris = memories_by_user(seeded, "chris")
        alice = memories_by_user(seeded, "alice")
        assert {r["user_id"] for r in chris} == {"chris"}
        assert {r["user_id"] for r in alice} == {"alice"}

    def test_state_filter(self, seeded: sqlite3.Connection) -> None:
        active = memories_by_user(seeded, "chris", state="active")
        archived = memories_by_user(seeded, "chris", state="archived")
        assert {r["vault_path"] for r in active} == {"a.md"}
        assert {r["vault_path"] for r in archived} == {"b.md"}

    def test_no_state_filter_returns_all(self, seeded: sqlite3.Connection) -> None:
        rows = memories_by_user(seeded, "chris")
        assert len(rows) == 2


class TestClaimsByUser:
    def test_user_isolation(self, seeded: sqlite3.Connection) -> None:
        rows = claims_by_user(seeded, "chris")
        assert len(rows) == 3
        assert all(r["user_id"] == "chris" for r in rows)

    def test_empty_for_unknown_user(self, seeded: sqlite3.Connection) -> None:
        assert claims_by_user(seeded, "nobody") == []


class TestClaimsBySubject:
    def test_exact_subject_match(self, seeded: sqlite3.Connection) -> None:
        rows = claims_by_subject(seeded, "chris", "chris")
        assert {r["object"] for r in rows} == {"coffee", "tea"}

    def test_subject_is_not_a_prefix_match(self, seeded: sqlite3.Connection) -> None:
        rows = claims_by_subject(seeded, "chris", "chr")
        assert rows == []


class TestContentHashLookup:
    def test_memory_by_hash_hit_and_miss(self, seeded: sqlite3.Connection) -> None:
        h = content_hash("a", "s1", "a.md")
        hit = memory_by_content_hash(seeded, h, user_id="chris")
        assert hit is not None
        assert hit["vault_path"] == "a.md"
        assert memory_by_content_hash(seeded, "deadbeef", user_id="chris") is None

    def test_claim_by_hash_hit_and_miss(self, seeded: sqlite3.Connection) -> None:
        h = content_hash("chris", "likes", "coffee", "direct:chris", "chris")
        hit = claim_by_content_hash(seeded, h, user_id="chris")
        assert hit is not None
        assert hit["object"] == "coffee"
        assert claim_by_content_hash(seeded, "nope", user_id="chris") is None
