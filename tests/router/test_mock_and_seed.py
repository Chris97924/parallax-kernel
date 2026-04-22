"""US-004: Tests for MockMemoryRouter and crosswalk_seed."""

from __future__ import annotations

from parallax.router.contracts import BackfillRequest, HealthReport, IngestRequest, QueryRequest
from parallax.router.crosswalk_seed import CROSSWALK_SEED, UnroutableQueryError, resolve, seed_hash
from parallax.router.mock_adapter import MockMemoryRouter
from parallax.router.ports import BackfillPort, IngestPort, InspectPort, QueryPort
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# MockMemoryRouter raises NotImplementedError on query/ingest/backfill
# ---------------------------------------------------------------------------


def test_query_raises_not_implemented() -> None:
    router = MockMemoryRouter()
    req = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1")
    try:
        router.query(req)
        raise AssertionError("Expected NotImplementedError")
    except NotImplementedError as exc:
        assert "Lane D-1 freeze" in str(exc)


def test_ingest_raises_not_implemented() -> None:
    router = MockMemoryRouter()
    req = IngestRequest(user_id="u1", kind="memory", payload={"body": "hi"})
    try:
        router.ingest(req)
        raise AssertionError("Expected NotImplementedError")
    except NotImplementedError as exc:
        assert "Lane D-1 freeze" in str(exc)


def test_backfill_raises_not_implemented() -> None:
    router = MockMemoryRouter()
    req = BackfillRequest(user_id="u1", crosswalk_version="v1")
    try:
        router.backfill(req)
        raise AssertionError("Expected NotImplementedError")
    except NotImplementedError as exc:
        assert "Lane D-1 freeze" in str(exc)


# ---------------------------------------------------------------------------
# MockMemoryRouter.health() returns a real HealthReport
# ---------------------------------------------------------------------------


def test_health_returns_health_report() -> None:
    router = MockMemoryRouter()
    report = router.health()
    assert isinstance(report, HealthReport)
    assert report.ok is True


def test_health_ports_registered() -> None:
    router = MockMemoryRouter()
    report = router.health()
    assert report.ports_registered == ("QueryPort", "IngestPort", "InspectPort", "BackfillPort")


# ---------------------------------------------------------------------------
# Structural isinstance checks (runtime_checkable)
# ---------------------------------------------------------------------------


def test_mock_is_query_port() -> None:
    assert isinstance(MockMemoryRouter(), QueryPort)


def test_mock_is_ingest_port() -> None:
    assert isinstance(MockMemoryRouter(), IngestPort)


def test_mock_is_inspect_port() -> None:
    assert isinstance(MockMemoryRouter(), InspectPort)


def test_mock_is_backfill_port() -> None:
    assert isinstance(MockMemoryRouter(), BackfillPort)


# ---------------------------------------------------------------------------
# Crosswalk seed
# ---------------------------------------------------------------------------


def test_crosswalk_seed_length() -> None:
    assert len(CROSSWALK_SEED) == 11


def test_resolve_intent_temporal() -> None:
    assert resolve("Intent.TEMPORAL") is QueryType.TEMPORAL_CONTEXT


def test_resolve_retrieve_kind_bug() -> None:
    assert resolve("RetrieveKind.bug") is QueryType.CHANGE_TRACE


def test_resolve_fallback_raises() -> None:
    try:
        resolve("Intent.FALLBACK")
        raise AssertionError("Expected UnroutableQueryError")
    except UnroutableQueryError:
        pass


def test_seed_hash_deterministic() -> None:
    h1 = seed_hash()
    h2 = seed_hash()
    assert h1 == h2


def test_seed_hash_length() -> None:
    assert len(seed_hash()) == 64
