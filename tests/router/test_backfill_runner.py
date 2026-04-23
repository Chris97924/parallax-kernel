"""US-002: Tests for BackfillRunner — read-only enumeration + zero-write invariant."""

from __future__ import annotations

import sqlite3

import pytest

from parallax.ingest import ingest_claim, ingest_memory
from parallax.migrations import migrate_to_latest

# Intentional private import for unit coverage
from parallax.router.backfill import (
    BackfillRunner,
    _classify_claim_predicate,  # noqa: PLC2701
)
from parallax.router.contracts import BackfillReport, BackfillRequest
from parallax.sqlite_store import connect

# `conn` fixture is provided by tests/conftest.py with proper try/finally
# teardown — do not redefine locally (SF1 fix from Lane D-2 python review).

_USER = "test_user_002"


# ---------------------------------------------------------------------------
# dry_run=False performs crosswalk writes (core tables remain read-only)
# ---------------------------------------------------------------------------


def test_dry_run_false_writes_crosswalk(conn: sqlite3.Connection) -> None:
    ingest_claim(
        conn,
        user_id=_USER,
        subject="stack",
        predicate="decision:choose-stack",
        object_="python",
    )
    ingest_memory(
        conn,
        user_id=_USER,
        title="Memo A",
        summary="summary a",
        vault_path="a.md",
    )

    before_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    before_memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before_decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    runner = BackfillRunner(conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned3_seed_v1",
        dry_run=False,
        scope="sample",
    )
    report = runner.run(req)
    assert report.writes_performed >= 2

    after_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    after_memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    after_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    after_decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]

    assert (before_claims, before_memories, before_events, before_decisions) == (
        after_claims,
        after_memories,
        after_events,
        after_decisions,
    )

    crosswalk_rows = conn.execute(
        "SELECT COUNT(*) FROM crosswalk WHERE user_id = ?",
        (_USER,),
    ).fetchone()[0]
    assert crosswalk_rows >= 2


def test_dry_run_false_persists_crosswalk_after_reopen(tmp_path) -> None:
    db_path = tmp_path / "backfill_persist.db"
    conn = connect(db_path)
    try:
        migrate_to_latest(conn)
        ingest_claim(
            conn,
            user_id=_USER,
            subject="stack",
            predicate="decision:choose-stack",
            object_="python",
        )
        ingest_memory(
            conn,
            user_id=_USER,
            title="Memo A",
            summary="summary a",
            vault_path="a.md",
        )

        runner = BackfillRunner(conn)
        req = BackfillRequest(
            user_id=_USER,
            crosswalk_version="laned3_seed_v1",
            dry_run=False,
            scope="sample",
        )
        report = runner.run(req)
        assert report.writes_performed >= 2
    finally:
        conn.close()

    reopened = connect(db_path)
    try:
        persisted = reopened.execute(
            "SELECT COUNT(*) FROM crosswalk WHERE user_id = ?",
            (_USER,),
        ).fetchone()[0]
    finally:
        reopened.close()

    assert persisted >= 2


# ---------------------------------------------------------------------------
# Empty DB → rows_examined=0, all zeros
# ---------------------------------------------------------------------------


def test_empty_db_scope_sample(conn: sqlite3.Connection) -> None:
    runner = BackfillRunner(conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned3_seed_v1",
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
        crosswalk_version="laned3_seed_v1",
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

    monkeypatch.setattr(_backfill_mod, "_core_fingerprint", _fake_fingerprint)

    runner = BackfillRunner(conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned3_seed_v1",
        dry_run=True,
        scope="sample",
    )
    with pytest.raises(RuntimeError) as exc_info:
        runner.run(req)

    msg = str(exc_info.value)
    assert "a" * 16 in msg
    assert "b" * 16 in msg
    assert "read-only core invariant" in msg


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
