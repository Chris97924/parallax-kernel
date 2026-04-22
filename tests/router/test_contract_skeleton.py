"""US-005: Contract-test skeleton — xfail gates for frozen port methods.

The three raising methods (query/ingest/backfill) are marked strict xfail so
if a future accidental implementation stops raising, the suite turns red.
health() is the one method that works — tested with a non-xfail test.
"""

from __future__ import annotations

import pytest

from parallax.router.contracts import BackfillRequest, IngestRequest, QueryRequest
from parallax.router.mock_adapter import MockMemoryRouter
from parallax.router.types import QueryType


@pytest.mark.xfail(
    strict=True,
    reason="Lane D-1 freeze: real adapter arrives in Lane D-2; mock deliberately raises",
)
def test_query_xfail() -> None:
    router = MockMemoryRouter()
    req = QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id="u1")
    router.query(req)  # expected to raise NotImplementedError -> xfail


@pytest.mark.xfail(
    strict=True,
    reason="Lane D-1 freeze: real adapter arrives in Lane D-2; mock deliberately raises",
)
def test_ingest_xfail() -> None:
    router = MockMemoryRouter()
    req = IngestRequest(user_id="u1", kind="memory", payload={"body": "hi"})
    router.ingest(req)  # expected to raise NotImplementedError -> xfail


@pytest.mark.xfail(
    strict=True,
    reason="Lane D-1 freeze: real adapter arrives in Lane D-2; mock deliberately raises",
)
def test_backfill_xfail() -> None:
    router = MockMemoryRouter()
    req = BackfillRequest(user_id="u1", crosswalk_version="v1")
    router.backfill(req)  # expected to raise NotImplementedError -> xfail


def test_health_works() -> None:
    """health() is the one port method that works in frozen mode."""
    report = MockMemoryRouter().health()
    assert report.ok is True
    assert report.query_type_count == 5
    assert report.ports_registered == ("QueryPort", "IngestPort", "InspectPort", "BackfillPort")
