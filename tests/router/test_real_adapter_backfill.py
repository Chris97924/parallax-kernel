"""US-D3-02: Tests for RealMemoryRouter.backfill wiring to BackfillRunner.

Coverage of PRD addendum AC:
- dry_run=True writes_performed == 0 (delegates dry-run invariant)
- dry_run=False writes to crosswalk after a fresh ingest
- index-selectivity check: EXPLAIN QUERY PLAN for representative SELECT uses
  idx_crosswalk_user_state (no full table scan) after >=1k rows.
"""

from __future__ import annotations

import sqlite3

from parallax.router.contracts import (
    BackfillReport,
    BackfillRequest,
    IngestRequest,
)
from parallax.router.real_adapter import RealMemoryRouter

_USER = "test_user_d3_02"

# Per-test seed counts. Named so a future schema change to memory/claim
# row layout doesn't leave bare integer assertions to grep.
_DRY_RUN_MEMORY_ROWS = 5
_DRY_RUN_CLAIM_ROWS = 5
_DRY_RUN_TOTAL_ROWS = _DRY_RUN_MEMORY_ROWS + _DRY_RUN_CLAIM_ROWS

_REAL_RUN_MEMORY_ROWS = 3
_REAL_RUN_CLAIM_ROWS = 3
_REAL_RUN_TOTAL_ROWS = _REAL_RUN_MEMORY_ROWS + _REAL_RUN_CLAIM_ROWS

# Cross the >=1k threshold for the EXPLAIN QUERY PLAN test (PRD addendum).
_INDEX_SELECTIVITY_ROWS = 1100


def _ingest_one_memory(router: RealMemoryRouter, *, idx: int) -> None:
    router.ingest(
        IngestRequest(
            user_id=_USER,
            kind="memory",
            payload={
                "body": f"memory-{idx}",
                "title": f"t-{idx}",
                "vault_path": f"v/{idx}.md",
            },
        )
    )


def _ingest_one_claim(router: RealMemoryRouter, *, idx: int) -> None:
    router.ingest(
        IngestRequest(
            user_id=_USER,
            kind="claim",
            payload={
                "subject": f"subj-{idx}",
                "predicate": "decision:choose",
                "object_": f"obj-{idx}",
            },
        )
    )


def test_backfill_returns_backfill_report(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    _ingest_one_memory(router, idx=0)
    report = router.backfill(
        BackfillRequest(user_id=_USER, crosswalk_version="v1", dry_run=True, scope="sample")
    )
    assert isinstance(report, BackfillReport)


def test_backfill_dry_run_writes_zero(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    for i in range(_DRY_RUN_MEMORY_ROWS):
        _ingest_one_memory(router, idx=i)
    for i in range(_DRY_RUN_CLAIM_ROWS):
        _ingest_one_claim(router, idx=i)

    report = router.backfill(
        BackfillRequest(user_id=_USER, crosswalk_version="v1", dry_run=True, scope="sample")
    )
    assert report.writes_performed == 0
    assert report.rows_examined == _DRY_RUN_TOTAL_ROWS


def test_backfill_real_run_persists_to_crosswalk(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    for i in range(_REAL_RUN_MEMORY_ROWS):
        _ingest_one_memory(router, idx=i)
    for i in range(_REAL_RUN_CLAIM_ROWS):
        _ingest_one_claim(router, idx=i)

    report = router.backfill(
        BackfillRequest(user_id=_USER, crosswalk_version="v1", dry_run=False, scope="sample")
    )
    assert report.writes_performed == _REAL_RUN_TOTAL_ROWS
    crosswalk_count = conn.execute(
        "SELECT COUNT(*) AS n FROM crosswalk WHERE user_id = ?", (_USER,)
    ).fetchone()["n"]
    assert crosswalk_count == _REAL_RUN_TOTAL_ROWS


def test_backfill_index_selectivity_user_state(conn: sqlite3.Connection) -> None:
    """ADDENDUM: EXPLAIN QUERY PLAN confirms idx_crosswalk_user_state usage."""
    router = RealMemoryRouter(conn)
    # scope='all' (cap 10_000) and 1100 rows crosses the >=1k threshold.
    for i in range(_INDEX_SELECTIVITY_ROWS):
        _ingest_one_memory(router, idx=i)

    router.backfill(
        BackfillRequest(user_id=_USER, crosswalk_version="v1", dry_run=False, scope="all")
    )

    plan_rows = conn.execute(
        "EXPLAIN QUERY PLAN " "SELECT canonical_ref FROM crosswalk WHERE user_id = ? AND state = ?",
        (_USER, "mapped"),
    ).fetchall()
    # row["detail"] (Row factory set in connect()) is more robust than row[3]
    # across SQLite versions (db-expert LOW-2 fix).
    plan_text = " ".join(str(row["detail"]) for row in plan_rows)
    assert (
        "idx_crosswalk_user_state" in plan_text
    ), f"Expected idx_crosswalk_user_state in plan, got: {plan_text}"
    # Defense in depth: never accept a full SCAN on a 1k-row table.
    assert "SCAN crosswalk" not in plan_text, f"Full table scan detected: {plan_text}"
