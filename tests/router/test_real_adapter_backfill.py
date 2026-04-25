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
    for i in range(5):
        _ingest_one_memory(router, idx=i)
        _ingest_one_claim(router, idx=i)

    report = router.backfill(
        BackfillRequest(user_id=_USER, crosswalk_version="v1", dry_run=True, scope="sample")
    )
    assert report.writes_performed == 0
    assert report.rows_examined == 10


def test_backfill_real_run_persists_to_crosswalk(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    for i in range(3):
        _ingest_one_memory(router, idx=i)
        _ingest_one_claim(router, idx=i)

    report = router.backfill(
        BackfillRequest(user_id=_USER, crosswalk_version="v1", dry_run=False, scope="sample")
    )
    assert report.writes_performed == 6
    crosswalk_count = conn.execute(
        "SELECT COUNT(*) AS n FROM crosswalk WHERE user_id = ?", (_USER,)
    ).fetchone()["n"]
    assert crosswalk_count == 6


def test_backfill_index_selectivity_user_state(conn: sqlite3.Connection) -> None:
    """ADDENDUM: EXPLAIN QUERY PLAN confirms idx_crosswalk_user_state usage."""
    router = RealMemoryRouter(conn)
    # Use scope='all' (cap 10_000) and 1100 rows to cross the >=1k threshold.
    for i in range(1100):
        _ingest_one_memory(router, idx=i)

    router.backfill(
        BackfillRequest(user_id=_USER, crosswalk_version="v1", dry_run=False, scope="all")
    )

    plan_rows = conn.execute(
        "EXPLAIN QUERY PLAN " "SELECT canonical_ref FROM crosswalk WHERE user_id = ? AND state = ?",
        (_USER, "mapped"),
    ).fetchall()
    plan_text = " ".join(str(row[3]) for row in plan_rows)
    assert (
        "idx_crosswalk_user_state" in plan_text
    ), f"Expected idx_crosswalk_user_state in plan, got: {plan_text}"
    # Defense in depth: never accept a full SCAN on a 1k-row table.
    assert "SCAN crosswalk" not in plan_text, f"Full table scan detected: {plan_text}"
