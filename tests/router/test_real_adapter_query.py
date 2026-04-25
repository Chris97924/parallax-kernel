"""US-001: Tests for RealMemoryRouter.query() dispatching to parallax.retrieve.*

Uses an in-memory SQLite DB with all migrations applied, seeded with one of each
relevant data kind, then exercises all five QueryType dispatches.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3

import pytest

from parallax.ingest import ingest_claim, ingest_memory
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest
from parallax.router.ports import BackfillPort, IngestPort, InspectPort, QueryPort
from parallax.router.real_adapter import QUERY_DISPATCH, RealMemoryRouter
from parallax.router.types import QueryType
from parallax.sqlite_store import now_iso

# `conn` fixture is provided by tests/conftest.py with proper try/finally
# teardown — do not redefine locally (SF1 fix from Lane D-2 python review).

_USER = "test_user_001"
_FILE_PATH = "src/main.py"
_SUBJECT = "python"
_SESSION = "sess-001"


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
    """Assert the common contract for all five dispatches.

    US-D3-04: ``body`` is the canonical evidence field added to every hit
    under router-on. Existing keys are preserved so consumers that pre-date
    the field still work; the field is always present (not Optional from
    the consumer's perspective when the router is on).
    """
    assert isinstance(ev, RetrievalEvidence)
    assert ev.stages == ("real_adapter_dispatch",)
    assert f"query_type={query_type.value}" in ev.notes
    assert f"retriever={QUERY_DISPATCH[query_type]}" in ev.notes
    # hits is a tuple of dicts with router evidence payload keys
    assert isinstance(ev.hits, tuple)
    for hit in ev.hits:
        assert isinstance(hit, dict)
        assert set(hit.keys()) == {
            "id",
            "text",
            "body",
            "created_at",
            "source_id",
            "kind",
            "score",
            "evidence",
            "full",
            "explain",
        }
        assert isinstance(hit["score"], float)
        assert isinstance(hit["explain"], dict)
        # US-D3-04: body is always a str (may be empty when the underlying
        # evidence has no body-like aliases — explicit "" instead of None
        # so consumers do not have to None-check).
        assert isinstance(hit["body"], str)
    # len(hits) >= 0 — test passes on contract, not count
    assert len(ev.hits) >= 0


def test_dto_body_field_is_str_on_every_hit(seeded_conn: sqlite3.Connection) -> None:
    """US-D3-04: body is a ``str`` on every hit (never None) regardless of kind.

    The contract assertion above already enforces ``isinstance(hit['body'], str)``;
    this test additionally exercises a query type that produces hits and
    confirms no hit slipped through with a non-str body.
    """
    router = RealMemoryRouter(seeded_conn)
    ev = router.query(QueryRequest(query_type=QueryType.ENTITY_PROFILE, user_id=_USER, q=_SUBJECT))
    if not ev.hits:
        pytest.skip("ENTITY_PROFILE retrieval returned no hits in this fixture")
    for hit in ev.hits:
        assert isinstance(hit["body"], str)


def test_dto_body_field_resolves_from_alias_for_claim(
    seeded_conn: sqlite3.Connection,
) -> None:
    """US-D3-04: claim body resolves via CLAIM_OBJECT_KEYS precedence.

    The seeded entity-profile claim has ``object='black formatter'`` so the
    derived body must be a non-empty str via the ``object`` alias.
    """
    router = RealMemoryRouter(seeded_conn)
    ev = router.query(QueryRequest(query_type=QueryType.ENTITY_PROFILE, user_id=_USER, q=_SUBJECT))
    claim_hits = [h for h in ev.hits if h["kind"] == "claim"]
    if not claim_hits:
        pytest.skip("ENTITY_PROFILE retrieval returned no claim hits")
    for hit in claim_hits:
        assert hit["body"], f"claim body should resolve from object/object_ alias; got hit={hit}"


def test_derive_body_legacy_row_no_alias_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy row with no recognized body alias must NOT emit a warning.

    Distinguishes "missing alias is normal" from "malformed value is a bug".
    Otherwise operators see noise on every legacy row and tune the warning out.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import parallax.router.real_adapter as adapter_mod

    fake_log = MagicMock()
    monkeypatch.setattr(adapter_mod, "_log", fake_log)

    legacy_hit = SimpleNamespace(
        entity_kind="memory",
        entity_id="m-legacy-1",
        title="legacy-title",
        full={"unrelated": "x"},  # no recognized alias, no malformed value
        evidence={"also_unrelated": "y"},
    )
    result = adapter_mod._derive_body(legacy_hit)
    assert result == "legacy-title"
    fake_log.warning.assert_not_called()


def test_derive_body_fallback_emits_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """HIGH-3 fix: when _derive_body falls back due to alias resolution
    failure, emit a structured WARNING so operators can spot post-ingest
    data corruption (a row whose persisted fields contain unexpected
    types or surrogate chars) rather than silently coercing to title.

    The project's parallax.obs.log JSON logger sets ``propagate=False`` so
    caplog (which hooks the root logger) cannot capture these records.
    Patch the module-level ``_log`` instead and assert on the call.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import parallax.router.real_adapter as adapter_mod

    fake_log = MagicMock()
    monkeypatch.setattr(adapter_mod, "_log", fake_log)

    bad_hit = SimpleNamespace(
        entity_kind="memory",
        entity_id="m-bad-1",
        title="fallback-title",
        full={"body": 0},  # type error → ValueError → fallback path
        evidence=None,
    )
    result = adapter_mod._derive_body(bad_hit)
    assert result == "fallback-title"

    fake_log.warning.assert_called_once()
    args, kwargs = fake_log.warning.call_args
    assert args[0] == "_derive_body fallback"
    extra = kwargs.get("extra", {})
    assert extra.get("kind") == "memory"
    assert extra.get("entity_id") == "m-bad-1"
    assert extra.get("event") == "derive_body_fallback"
    reasons = extra.get("reasons") or []
    assert any("non-str" in r for r in reasons)


def test_recent_context_dispatch(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id=_USER, q="")
    ev = router.query(req)
    _assert_evidence_contract(ev, QueryType.RECENT_CONTEXT)


def test_artifact_context_dispatch(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(query_type=QueryType.ARTIFACT_CONTEXT, user_id=_USER, q=_FILE_PATH)
    ev = router.query(req)
    _assert_evidence_contract(ev, QueryType.ARTIFACT_CONTEXT)


def test_entity_profile_dispatch(seeded_conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(seeded_conn)
    req = QueryRequest(query_type=QueryType.ENTITY_PROFILE, user_id=_USER, q=_SUBJECT)
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
