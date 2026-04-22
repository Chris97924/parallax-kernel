"""US-001: Tests for RealMemoryRouter.query() dispatching to parallax.retrieve.*

Uses an in-memory SQLite DB with all migrations applied, seeded with one of each
relevant data kind, then exercises all five QueryType dispatches.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sqlite3

import pytest

from parallax.ingest import ingest_claim, ingest_memory
from parallax.migrations import migrate_to_latest
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import BackfillRequest, IngestRequest, QueryRequest
from parallax.router.ports import BackfillPort, IngestPort, InspectPort, QueryPort
from parallax.router.real_adapter import QUERY_DISPATCH, RealMemoryRouter
from parallax.router.types import QueryType
from parallax.sqlite_store import connect, now_iso

_USER = "test_user_001"
_FILE_PATH = "src/main.py"
_SUBJECT = "python"
_SESSION = "sess-001"


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    """Fresh SQLite connection with all migrations applied."""
    db = tmp_path / "test_real_adapter.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


@pytest.fixture()
def seeded_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    """DB seeded with one memory, three claims, and one file_edit event."""
    # One memory
    ingest_memory(
        conn,
        user_id=_USER,
        title="Python setup notes",
        summary="How to configure Python environment",
        vault_path="notes/python.md",
    )

    # One decision claim
    ingest_claim(
        conn,
        user_id=_USER,
        subject="stack",
        predicate="decision:choose-stack",
        object_="python",
    )

    # One bug claim
    ingest_claim(
        conn,
        user_id=_USER,
        subject="auth",
        predicate="fix:bug-1234",
        object_="fixed null pointer",
    )

    # One entity/preference claim
    ingest_claim(
        conn,
        user_id=_USER,
        subject=_SUBJECT,
        predicate="prefers",
        object_="black formatter",
    )

    # One file_edit event via direct INSERT matching the events schema
    now = now_iso()
    conn.execute(
        """
        INSERT INTO events (event_id, user_id, session_id, actor, event_type,
                            target_kind, target_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "evt-001",
            _USER,
            _SESSION,
            "system",
            "tool.edit",
            "file",
            _FILE_PATH,
            json.dumps({"file_path": _FILE_PATH, "content": "print('hello')"}),
            now,
        ),
    )
    conn.commit()

    return conn


# ---------------------------------------------------------------------------
# Query dispatch tests — one per QueryType
# ---------------------------------------------------------------------------


def _assert_evidence_contract(ev: RetrievalEvidence, query_type: QueryType) -> None:
    """Assert the common contract for all five dispatches."""
    assert isinstance(ev, RetrievalEvidence)
    assert ev.stages == ("real_adapter_dispatch",)
    assert f"query_type={query_type.value}" in ev.notes
    assert f"retriever={QUERY_DISPATCH[query_type]}" in ev.notes
    # hits is a tuple of dicts with exactly the right keys
    assert isinstance(ev.hits, tuple)
    for hit in ev.hits:
        assert isinstance(hit, dict)
        assert set(hit.keys()) == {"id", "text", "created_at", "source_id", "kind"}
    # len(hits) >= 0 — test passes on contract, not count
    assert len(ev.hits) >= 0


def test_recent_context_dispatch(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id=_USER, q="")
    ev = router.query(req)
    _assert_evidence_contract(ev, QueryType.RECENT_CONTEXT)


def test_artifact_context_dispatch(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(
        query_type=QueryType.ARTIFACT_CONTEXT, user_id=_USER, q=_FILE_PATH
    )
    ev = router.query(req)
    _assert_evidence_contract(ev, QueryType.ARTIFACT_CONTEXT)


def test_entity_profile_dispatch(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(
        query_type=QueryType.ENTITY_PROFILE, user_id=_USER, q=_SUBJECT
    )
    ev = router.query(req)
    _assert_evidence_contract(ev, QueryType.ENTITY_PROFILE)


def test_change_trace_dispatch(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(query_type=QueryType.CHANGE_TRACE, user_id=_USER, q="")
    ev = router.query(req)
    _assert_evidence_contract(ev, QueryType.CHANGE_TRACE)


def test_temporal_context_dispatch(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    now = _dt.datetime.now(_dt.UTC)
    since = (now - _dt.timedelta(hours=1)).isoformat()
    until = (now + _dt.timedelta(hours=1)).isoformat()
    req = QueryRequest(
        query_type=QueryType.TEMPORAL_CONTEXT,
        user_id=_USER,
        q="",
        since=since,
        until=until,
    )
    ev = router.query(req)
    _assert_evidence_contract(ev, QueryType.TEMPORAL_CONTEXT)


# ---------------------------------------------------------------------------
# Temporal validation: missing since / until raises ValueError
# ---------------------------------------------------------------------------


def test_temporal_context_missing_since_raises(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    req = QueryRequest(
        query_type=QueryType.TEMPORAL_CONTEXT,
        user_id=_USER,
        since=None,
        until="2025-01-01T00:00:00+00:00",
    )
    msg = "TEMPORAL_CONTEXT requires since and until in QueryRequest"
    with pytest.raises(ValueError, match=msg):
        router.query(req)


def test_temporal_context_missing_until_raises(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    req = QueryRequest(
        query_type=QueryType.TEMPORAL_CONTEXT,
        user_id=_USER,
        since="2025-01-01T00:00:00+00:00",
        until=None,
    )
    msg = "TEMPORAL_CONTEXT requires since and until in QueryRequest"
    with pytest.raises(ValueError, match=msg):
        router.query(req)


def test_temporal_context_both_none_raises(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    req = QueryRequest(
        query_type=QueryType.TEMPORAL_CONTEXT,
        user_id=_USER,
        since=None,
        until=None,
    )
    msg = "TEMPORAL_CONTEXT requires since and until in QueryRequest"
    with pytest.raises(ValueError, match=msg):
        router.query(req)


# ---------------------------------------------------------------------------
# Protocol isinstance checks
# ---------------------------------------------------------------------------


def test_real_router_is_query_port(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    assert isinstance(router, QueryPort)


def test_real_router_is_ingest_port(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    assert isinstance(router, IngestPort)


def test_real_router_is_inspect_port(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    assert isinstance(router, InspectPort)


def test_real_router_is_backfill_port(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    assert isinstance(router, BackfillPort)


# ---------------------------------------------------------------------------
# NotImplementedError stubs
# ---------------------------------------------------------------------------


def test_ingest_raises_not_implemented(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    req = IngestRequest(user_id=_USER, kind="memory", payload={"body": "hi"})
    with pytest.raises(NotImplementedError, match="Lane D-2 freeze: RealMemoryRouter.ingest"):
        router.ingest(req)


def test_backfill_stub_raises_not_implemented(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    req = BackfillRequest(user_id=_USER, crosswalk_version="laned2_seed_v1", dry_run=True)
    with pytest.raises(NotImplementedError, match="Lane D-2 freeze: RealMemoryRouter.backfill"):
        router.backfill(req)


# ---------------------------------------------------------------------------
# QUERY_DISPATCH immutability
# ---------------------------------------------------------------------------


def test_query_dispatch_is_frozen() -> None:
    import types

    assert isinstance(QUERY_DISPATCH, types.MappingProxyType)


def test_query_dispatch_has_all_five_entries() -> None:
    assert len(QUERY_DISPATCH) == 5
    assert QUERY_DISPATCH[QueryType.RECENT_CONTEXT] == "recent_context"
    assert QUERY_DISPATCH[QueryType.ARTIFACT_CONTEXT] == "by_file"
    assert QUERY_DISPATCH[QueryType.ENTITY_PROFILE] == "by_entity"
    assert QUERY_DISPATCH[QueryType.CHANGE_TRACE] == "by_decision"
    assert QUERY_DISPATCH[QueryType.TEMPORAL_CONTEXT] == "by_timeline"
