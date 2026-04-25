"""Tests for ingest_memory_with_status / ingest_claim_with_status (Lane D-3 US-D3-01).

These helpers expose the (persisted_id, deduped) tuple that the existing
ingest_memory / ingest_claim hide internally. Router IngestResult.deduped
is derived from this without a TOCTOU-prone pre-check.
"""

from __future__ import annotations

import sqlite3

from parallax.ingest import ingest_claim_with_status, ingest_memory_with_status


def test_ingest_memory_with_status_first_call_not_deduped(conn: sqlite3.Connection) -> None:
    mid, deduped = ingest_memory_with_status(
        conn,
        user_id="alice",
        title="t",
        summary="hello",
        vault_path="/vault/m1",
    )
    assert isinstance(mid, str) and len(mid) > 0
    assert deduped is False


def test_ingest_memory_with_status_second_call_deduped(conn: sqlite3.Connection) -> None:
    mid_a, dedup_a = ingest_memory_with_status(
        conn,
        user_id="alice",
        title="t",
        summary="hello",
        vault_path="/vault/m1",
    )
    mid_b, dedup_b = ingest_memory_with_status(
        conn,
        user_id="alice",
        title="t",
        summary="hello",
        vault_path="/vault/m1",
    )
    assert mid_a == mid_b
    assert dedup_a is False
    assert dedup_b is True


def test_ingest_claim_with_status_first_call_not_deduped(conn: sqlite3.Connection) -> None:
    cid, deduped = ingest_claim_with_status(
        conn,
        user_id="alice",
        subject="alice",
        predicate="likes",
        object_="coffee",
    )
    assert isinstance(cid, str) and len(cid) > 0
    assert deduped is False


def test_ingest_claim_with_status_second_call_deduped(conn: sqlite3.Connection) -> None:
    cid_a, dedup_a = ingest_claim_with_status(
        conn,
        user_id="alice",
        subject="alice",
        predicate="likes",
        object_="coffee",
    )
    cid_b, dedup_b = ingest_claim_with_status(
        conn,
        user_id="alice",
        subject="alice",
        predicate="likes",
        object_="coffee",
    )
    assert cid_a == cid_b
    assert dedup_a is False
    assert dedup_b is True


def test_ingest_memory_returns_id_only_unchanged(conn: sqlite3.Connection) -> None:
    """Backward compat: existing ingest_memory still returns just str."""
    from parallax.ingest import ingest_memory

    mid = ingest_memory(
        conn,
        user_id="alice",
        title="t",
        summary="legacy",
        vault_path="/vault/legacy",
    )
    assert isinstance(mid, str) and len(mid) > 0


def test_ingest_claim_returns_id_only_unchanged(conn: sqlite3.Connection) -> None:
    """Backward compat: existing ingest_claim still returns just str."""
    from parallax.ingest import ingest_claim

    cid = ingest_claim(
        conn,
        user_id="alice",
        subject="alice",
        predicate="likes",
        object_="tea",
    )
    assert isinstance(cid, str) and len(cid) > 0
