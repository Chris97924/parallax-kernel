"""US-002: Tests for the four capability port Protocols."""

from __future__ import annotations

import inspect
import typing

from parallax.router.contracts import (
    BackfillReport,
    BackfillRequest,
    HealthReport,
    IngestRequest,
    IngestResult,
    QueryRequest,
    RetrievalEvidence,
)
from parallax.router.mock_adapter import MockMemoryRouter
from parallax.router.ports import BackfillPort, IngestPort, InspectPort, QueryPort


def test_mock_is_query_port() -> None:
    assert isinstance(MockMemoryRouter(), QueryPort)


def test_mock_is_ingest_port() -> None:
    assert isinstance(MockMemoryRouter(), IngestPort)


def test_mock_is_inspect_port() -> None:
    assert isinstance(MockMemoryRouter(), InspectPort)


def test_mock_is_backfill_port() -> None:
    assert isinstance(MockMemoryRouter(), BackfillPort)


def test_query_port_signature() -> None:
    sig = inspect.signature(QueryPort.query)
    assert list(sig.parameters.keys()) == ["self", "request"]


def test_ingest_port_signature() -> None:
    sig = inspect.signature(IngestPort.ingest)
    assert list(sig.parameters.keys()) == ["self", "request"]


def test_inspect_port_signature() -> None:
    sig = inspect.signature(InspectPort.health)
    assert list(sig.parameters.keys()) == ["self"]


def test_backfill_port_signature() -> None:
    sig = inspect.signature(BackfillPort.backfill)
    assert list(sig.parameters.keys()) == ["self", "request"]


def test_query_port_type_hints() -> None:
    hints = typing.get_type_hints(QueryPort.query)
    assert hints["request"] is QueryRequest
    assert hints["return"] is RetrievalEvidence


def test_ingest_port_type_hints() -> None:
    hints = typing.get_type_hints(IngestPort.ingest)
    assert hints["request"] is IngestRequest
    assert hints["return"] is IngestResult


def test_inspect_port_type_hints() -> None:
    hints = typing.get_type_hints(InspectPort.health)
    assert hints["return"] is HealthReport


def test_backfill_port_type_hints() -> None:
    hints = typing.get_type_hints(BackfillPort.backfill)
    assert hints["request"] is BackfillRequest
    assert hints["return"] is BackfillReport
