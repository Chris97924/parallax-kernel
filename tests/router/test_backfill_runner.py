"""US-002: Tests for BackfillRunner — read-only enumeration + zero-write invariant."""

from __future__ import annotations

import sqlite3

import pytest

from parallax.ingest import ingest_claim, ingest_memory

# Intentional private import for unit coverage
from parallax.router.backfill import (
    BackfillRunner,
    _classify_claim_predicate,  # noqa: PLC2701
)
from parallax.router.contracts import BackfillReport, BackfillRequest

# `conn` fixture is provided by tests/conftest.py with proper try/finally
# teardown — do not redefine locally (SF1 fix from Lane D-2 python review).

_USER = "test_user_002"


# ---------------------------------------------------------------------------
# dry_run=False raises ValueError with exact message
# ---------------------------------------------------------------------------


def test_dry_run_false_raises_value_error(conn: sqlite3.Connection) -> None:
    runner = BackfillRunner(conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned2_seed_v1",
        dry_run=False,
        scope="sample",
    )
    with pytest.raises(
        ValueError,
        match="Lane D-2 BackfillRunner supports dry_run=True only",
    ):
        runner.run(req)


def test_dry_run_false_message_exact(conn: sqlite3.Connection) -> None:
    runner = BackfillRunner(conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned2_seed_v1",
        dry_run=False,
    )
    with pytest.raises(ValueError) as exc_info:
        runner.run(req)
    assert "real writes land in Lane D-3" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Empty DB → rows_examined=0, all zeros
# ---------------------------------------------------------------------------


def test_empty_db_scope_sample(conn: sqlite3.Connection) -> None:
    runner = BackfillRunner(conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned2_seed_v1",
        dry_run=True,
        scope="sample",
    )
    report = runner.run(req)
    assert isinstance(report, BackfillReport)
    assert report.rows_examined == 0
    assert report.rows_mapped == 0
    assert report.rows_unmapped == 0
    assert report.rows_conflict == 0
    assert report.writes_performed == 0
    assert report.arbitrations == ()


# ---------------------------------------------------------------------------
# Seeded DB: 3 claims + 2 memories → rows_examined=5, rows_mapped=5
# ---------------------------------------------------------------------------


def test_seeded_scope_sample(conn: sqlite3.Connection) -> None:
    # 3 claims: one decision:, one fix:, one prefers (entity)
    ingest_claim(
        conn,
        user_id=_USER,
        subject="stack",
        predicate="decision:choose-stack",
        object_="python",
    )
    ingest_claim(
        conn,
        user_id=_USER,
        subject="auth",
        predicate="fix:bug-999",
        object_="fixed",
    )
    ingest_claim(
        conn,
        user_id=_USER,
        subject="editor",
        predicate="prefers",
        object_="vim",
    )
    # 2 memories
    ingest_memory(
        conn,
        user_id=_USER,
        title="Memo A",
        summary="summary a",
        vault_path="a.md",
    )
    ingest_memory(
        conn,
        user_id=_USER,
        title="Memo B",
        summary="summary b",
        vault_path="b.md",
    )

    runner = BackfillRunner(conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned2_seed_v1",
        dry_run=True,
        scope="sample",
    )
    report = runner.run(req)

    assert report.rows_examined == 5
    assert report.rows_mapped == 5
    assert report.rows_unmapped == 0
    assert report.rows_conflict == 0
    assert report.writes_performed == 0


# ---------------------------------------------------------------------------
# Zero-write invariant enforcement via monkeypatch
# ---------------------------------------------------------------------------


def test_zero_write_invariant_raises_on_fingerprint_mismatch(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch _write_fingerprint to return different values pre/post."""
    import parallax.router.backfill as _backfill_mod

    call_count = 0

    def _fake_fingerprint(_conn: sqlite3.Connection) -> str:
        nonlocal call_count
        call_count += 1
        return "a" * 64 if call_count == 1 else "b" * 64

    monkeypatch.setattr(_backfill_mod, "_write_fingerprint", _fake_fingerprint)

    runner = BackfillRunner(conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned2_seed_v1",
        dry_run=True,
        scope="sample",
    )
    with pytest.raises(RuntimeError) as exc_info:
        runner.run(req)

    msg = str(exc_info.value)
    assert "a" * 16 in msg
    assert "b" * 16 in msg
    assert "zero-write invariant" in msg


# ---------------------------------------------------------------------------
# _classify_claim_predicate unit tests — 6 cases
# ---------------------------------------------------------------------------


def test_classify_decision() -> None:
    assert _classify_claim_predicate("decision:x") == "RetrieveKind.decision"


def test_classify_fix_colon() -> None:
    assert _classify_claim_predicate("fix:x") == "RetrieveKind.bug"


def test_classify_bug_underscore_fix() -> None:
    assert _classify_claim_predicate("bug_fix:x") == "RetrieveKind.bug"


def test_classify_bugfix_uppercase() -> None:
    assert _classify_claim_predicate("BUGFIX:x") == "RetrieveKind.bug"


def test_classify_prefers_entity() -> None:
    assert _classify_claim_predicate("prefers") == "RetrieveKind.entity"


def test_classify_unknown_entity() -> None:
    assert _classify_claim_predicate("unknown") == "RetrieveKind.entity"
