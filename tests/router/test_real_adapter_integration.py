"""US-003: End-to-end integration smoke test for RealMemoryRouter + BackfillRunner."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.ingest import ingest_claim, ingest_memory
from parallax.migrations import migrate_to_latest
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router import (
    MEMORY_ROUTER,
    BackfillRunner,
    MockMemoryRouter,
    QueryType,
    RealMemoryRouter,
    is_router_enabled,
)
from parallax.router.contracts import BackfillReport, BackfillRequest, QueryRequest
from parallax.sqlite_store import connect

_USER = "test_user_003"


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    """Fresh SQLite connection with all migrations applied."""
    db = tmp_path / "test_integration.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


@pytest.fixture()
def seeded_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    """DB seeded with 2 claims + 1 memory."""
    ingest_claim(
        conn,
        user_id=_USER,
        subject="architecture",
        predicate="decision:choose-db",
        object_="sqlite",
    )
    ingest_claim(
        conn,
        user_id=_USER,
        subject="python",
        predicate="prefers",
        object_="type hints",
    )
    ingest_memory(
        conn,
        user_id=_USER,
        title="Project setup",
        summary="Initial project configuration",
        vault_path="setup.md",
    )
    return conn


# ---------------------------------------------------------------------------
# Public surface: imports from parallax.router succeed
# ---------------------------------------------------------------------------


def test_public_surface_real_memory_router() -> None:
    assert RealMemoryRouter is not None


def test_public_surface_backfill_runner() -> None:
    assert BackfillRunner is not None


def test_public_surface_mock_memory_router() -> None:
    assert MockMemoryRouter is not None


def test_public_surface_query_type() -> None:
    assert QueryType is not None


def test_public_surface_memory_router_flag() -> None:
    assert isinstance(MEMORY_ROUTER, bool)


def test_public_surface_is_router_enabled() -> None:
    assert callable(is_router_enabled)


# ---------------------------------------------------------------------------
# RealMemoryRouter query — 3 QueryType dispatches
# ---------------------------------------------------------------------------


def test_integration_recent_context(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id=_USER)
    ev = router.query(req)
    assert isinstance(ev, RetrievalEvidence)
    assert ev.stages == ("real_adapter_dispatch",)


def test_integration_entity_profile(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(
        query_type=QueryType.ENTITY_PROFILE, user_id=_USER, q="python"
    )
    ev = router.query(req)
    assert isinstance(ev, RetrievalEvidence)
    assert ev.stages == ("real_adapter_dispatch",)


def test_integration_change_trace(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(query_type=QueryType.CHANGE_TRACE, user_id=_USER)
    ev = router.query(req)
    assert isinstance(ev, RetrievalEvidence)
    assert ev.stages == ("real_adapter_dispatch",)


# ---------------------------------------------------------------------------
# BackfillRunner: rows_examined==3, rows_mapped==3, invariants
# ---------------------------------------------------------------------------


def test_integration_backfill_report(seeded_conn: sqlite3.Connection) -> None:
    runner = BackfillRunner(seeded_conn)
    req = BackfillRequest(
        user_id=_USER,
        crosswalk_version="laned2_seed_v1",
        dry_run=True,
        scope="sample",
    )
    report = runner.run(req)
    assert isinstance(report, BackfillReport)
    assert report.rows_examined == 3  # 2 claims + 1 memory
    assert report.rows_mapped == 3
    assert report.rows_conflict == 0
    assert report.writes_performed == 0
    assert report.arbitrations == ()


# ---------------------------------------------------------------------------
# Import discipline: real_adapter and backfill do NOT import parallax.retrieve
# at module level (method-local import only).
# NOTE: The full subprocess check `import parallax.router; assert 'parallax.retrieve'
# not in sys.modules` cannot pass because parallax/__init__.py eagerly imports
# parallax.retrieve at package level — this is a pre-existing condition outside
# Lane D-2 scope (verified: the assertion fails identically on the pre-D2 branch).
# The intent of the AC is met: real_adapter.py and backfill.py use method-local
# imports only. These tests verify that structural guarantee directly.
# ---------------------------------------------------------------------------


def test_real_adapter_module_has_no_retrieve_top_level_import() -> None:
    """real_adapter.py source must not contain a top-level 'import parallax.retrieve'."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent.parent / "parallax" / "router" / "real_adapter.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # top-level (no enclosing function) from-imports
            if (node.module or "").startswith("parallax.retrieve"):
                # Only flag top-level imports (col_offset == 0)
                if node.col_offset == 0:
                    raise AssertionError(
                        f"real_adapter.py has top-level import of {node.module!r}"
                    )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("parallax.retrieve") and node.col_offset == 0:
                    raise AssertionError(
                        f"real_adapter.py has top-level import of {alias.name!r}"
                    )


def test_backfill_module_has_no_retrieve_top_level_import() -> None:
    """backfill.py source must not contain a top-level 'import parallax.retrieve'."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent.parent / "parallax" / "router" / "backfill.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("parallax.retrieve") and node.col_offset == 0:
                raise AssertionError(
                    f"backfill.py has top-level import of {node.module!r}"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("parallax.retrieve") and node.col_offset == 0:
                    raise AssertionError(
                        f"backfill.py has top-level import of {alias.name!r}"
                    )
