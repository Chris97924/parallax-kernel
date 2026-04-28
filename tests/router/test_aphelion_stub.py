"""Tests for AphelionReadAdapter stub (M3-T1.2, US-011).

Verifies that the stub always raises AphelionUnreachableError("not_implemented")
and that it conforms to the QueryPort Protocol.
"""

from __future__ import annotations

import pytest

from parallax.router.aphelion_stub import AphelionReadAdapter, AphelionUnreachableError
from parallax.router.contracts import QueryRequest
from parallax.router.ports import QueryPort
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Stub always raises AphelionUnreachableError
# ---------------------------------------------------------------------------


def test_query_always_raises_unreachable() -> None:
    adapter = AphelionReadAdapter()
    request = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1")
    with pytest.raises(AphelionUnreachableError):
        adapter.query(request)


def test_unreachable_reason_is_not_implemented() -> None:
    adapter = AphelionReadAdapter()
    request = QueryRequest(query_type=QueryType.ENTITY_PROFILE, user_id="u1")
    try:
        adapter.query(request)
    except AphelionUnreachableError as exc:
        assert exc.reason == "not_implemented"
    else:
        pytest.fail("Expected AphelionUnreachableError to be raised")


# ---------------------------------------------------------------------------
# AphelionUnreachableError.reason attribute
# ---------------------------------------------------------------------------


def test_unreachable_error_reason_attribute() -> None:
    err = AphelionUnreachableError("timeout")
    assert err.reason == "timeout"
    assert "timeout" in str(err)


@pytest.mark.parametrize("reason", ["not_implemented", "timeout", "connection_error"])
def test_unreachable_error_reason_values(reason: str) -> None:
    err = AphelionUnreachableError(reason)
    assert err.reason == reason


# ---------------------------------------------------------------------------
# Conforms to QueryPort Protocol (runtime_checkable)
# ---------------------------------------------------------------------------


def test_conforms_to_query_port_protocol() -> None:
    adapter = AphelionReadAdapter()
    assert isinstance(adapter, QueryPort), "AphelionReadAdapter must conform to QueryPort Protocol"


# ---------------------------------------------------------------------------
# Constructor accepts base_url and timeout_ms
# ---------------------------------------------------------------------------


def test_constructor_defaults() -> None:
    adapter = AphelionReadAdapter()
    assert adapter._base_url is None
    assert adapter._timeout_ms == 100.0


def test_constructor_custom_args() -> None:
    adapter = AphelionReadAdapter(base_url="http://aphelion:8080", timeout_ms=50.0)
    assert adapter._base_url == "http://aphelion:8080"
    assert adapter._timeout_ms == 50.0


# ---------------------------------------------------------------------------
# All QueryType values raise unreachable (stub is query-type agnostic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("qt", list(QueryType))
def test_all_query_types_raise(qt: QueryType) -> None:
    adapter = AphelionReadAdapter()
    request = QueryRequest(query_type=qt, user_id="u1")
    with pytest.raises(AphelionUnreachableError):
        adapter.query(request)
