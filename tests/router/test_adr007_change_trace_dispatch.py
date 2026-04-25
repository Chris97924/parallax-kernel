"""ADR-007: CHANGE_TRACE payload-level dispatch via QueryRequest.params.legacy_kind.

AC-1: legacy_kind=="bug" -> by_bug_fix
AC-1: legacy_kind absent or "decision" -> by_decision
AC-4: debug log when legacy_kind is None
"""

from __future__ import annotations

import sqlite3

import pytest

from parallax.ingest import ingest_claim, ingest_memory
from parallax.router.contracts import QueryRequest
from parallax.router.real_adapter import RealMemoryRouter
from parallax.router.types import QueryType

_USER = "test_adr007"


@pytest.fixture()
def seeded_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    ingest_memory(
        conn,
        user_id=_USER,
        title="Python notes",
        summary="setup notes",
        vault_path="notes/py.md",
    )
    ingest_claim(
        conn,
        user_id=_USER,
        subject="stack",
        predicate="decision:choose-db",
        object_="sqlite",
    )
    ingest_claim(
        conn,
        user_id=_USER,
        subject="auth",
        predicate="fix:bug-1234",
        object_="fixed null pointer",
    )
    conn.commit()
    return conn


def test_change_trace_bug_kind_dispatches_to_by_bug_fix(
    seeded_conn: sqlite3.Connection,
) -> None:
    """AC-1: params.legacy_kind=='bug' must route to by_bug_fix."""
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(
        query_type=QueryType.CHANGE_TRACE,
        user_id=_USER,
        params={"legacy_kind": "bug"},
    )
    evidence = router.query(req)
    # by_bug_fix returns claims whose predicate matches ^(fix|bug[-_]?fix|bugfix)
    # The seeded bug claim has predicate "fix:bug-1234" which matches.
    assert evidence.hits, "expected at least one hit from by_bug_fix"
    # notes should reflect the actual retriever
    assert any("by_bug_fix" in n for n in evidence.notes), (
        f"notes should include by_bug_fix; got {evidence.notes}"
    )


def test_change_trace_decision_legacy_kind_uses_by_decision(
    seeded_conn: sqlite3.Connection,
) -> None:
    """AC-1: params.legacy_kind=='decision' routes to by_decision."""
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(
        query_type=QueryType.CHANGE_TRACE,
        user_id=_USER,
        params={"legacy_kind": "decision"},
    )
    evidence = router.query(req)
    assert any("by_decision" in n for n in evidence.notes), (
        f"notes should include by_decision; got {evidence.notes}"
    )


def test_change_trace_no_params_defaults_to_by_decision(
    seeded_conn: sqlite3.Connection,
) -> None:
    """AC-1 + AC-4: absent legacy_kind defaults to by_decision."""
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(
        query_type=QueryType.CHANGE_TRACE,
        user_id=_USER,
    )
    evidence = router.query(req)
    assert any("by_decision" in n for n in evidence.notes), (
        f"notes should include by_decision; got {evidence.notes}"
    )


def test_change_trace_empty_params_defaults_to_by_decision(
    seeded_conn: sqlite3.Connection,
) -> None:
    """params={} (no legacy_kind key) defaults to by_decision."""
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(
        query_type=QueryType.CHANGE_TRACE,
        user_id=_USER,
        params={},
    )
    evidence = router.query(req)
    assert any("by_decision" in n for n in evidence.notes)


def test_query_request_params_field_backward_compatible() -> None:
    """params defaults to None — existing call sites are unaffected."""
    req = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u")
    assert req.params is None


def test_change_trace_unknown_legacy_kind_defaults_to_by_decision(
    seeded_conn: sqlite3.Connection,
) -> None:
    """Unrecognized legacy_kind stays on by_decision path (documents design choice)."""
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(
        query_type=QueryType.CHANGE_TRACE,
        user_id=_USER,
        params={"legacy_kind": "nonsense"},
    )
    evidence = router.query(req)
    assert any("by_decision" in n for n in evidence.notes), (
        f"unknown legacy_kind should fall through to by_decision; got {evidence.notes}"
    )
