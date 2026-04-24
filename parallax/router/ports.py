"""Four capability ports as runtime_checkable Protocols (Lane D-1 signature freeze).

Dependency graph: types.py (no deps) <- contracts.py <- ports.py <- mock_adapter.py

All annotations use real imported types — no string forward references.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from parallax.router.contracts import (
    BackfillReport,
    BackfillRequest,
    HealthReport,
    IngestRequest,
    IngestResult,
    QueryRequest,
    RetrievalEvidence,
)

__all__ = ["QueryPort", "IngestPort", "InspectPort", "BackfillPort"]


@runtime_checkable
class QueryPort(Protocol):
    """Route a typed query request to evidence hits."""

    def query(self, request: QueryRequest) -> RetrievalEvidence: ...


@runtime_checkable
class IngestPort(Protocol):
    """Persist a memory / claim payload into the router store."""

    def ingest(self, request: IngestRequest) -> IngestResult: ...


@runtime_checkable
class InspectPort(Protocol):
    """Return the router's health and introspection snapshot."""

    def health(self) -> HealthReport: ...


@runtime_checkable
class BackfillPort(Protocol):
    """Run a Crosswalk-driven backfill; MUST honour request.dry_run."""

    def backfill(self, request: BackfillRequest) -> BackfillReport: ...
