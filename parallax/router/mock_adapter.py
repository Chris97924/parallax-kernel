"""MockMemoryRouter — stub implementation of all four port protocols (Lane D-1).

Every port method except health() raises NotImplementedError with a standard
message. health() returns a real HealthReport so /inspect never returns 500
even in frozen mode.
"""

from __future__ import annotations

from parallax.router.contracts import (
    BackfillReport,
    BackfillRequest,
    HealthReport,
    IngestRequest,
    IngestResult,
    QueryRequest,
    RetrievalEvidence,
)
from parallax.router.crosswalk_seed import seed_hash

__all__ = ["MockMemoryRouter"]

_PORTS = ("QueryPort", "IngestPort", "InspectPort", "BackfillPort")
_FREEZE_MSG = (
    "Lane D-1 freeze: MockMemoryRouter.{method} is intentionally unimplemented;"
    " real adapter lands in Lane D-2"
)


class MockMemoryRouter:
    """Structural implementation of QueryPort, IngestPort, InspectPort, BackfillPort.

    No explicit Protocol inheritance — duck-typed via @runtime_checkable.
    """

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        """Raise NotImplementedError; real adapter arrives in Lane D-2."""
        raise NotImplementedError(_FREEZE_MSG.format(method="query"))

    def ingest(self, request: IngestRequest) -> IngestResult:
        """Raise NotImplementedError; real adapter arrives in Lane D-2."""
        raise NotImplementedError(_FREEZE_MSG.format(method="ingest"))

    def backfill(self, request: BackfillRequest) -> BackfillReport:
        """Raise NotImplementedError; real adapter arrives in Lane D-2."""
        raise NotImplementedError(_FREEZE_MSG.format(method="backfill"))

    def health(self) -> HealthReport:
        """Return a real HealthReport — the one port method that works in freeze mode."""
        from parallax.router.config import MEMORY_ROUTER  # late import avoids circular

        return HealthReport(
            ok=True,
            flag_enabled=MEMORY_ROUTER,
            query_type_count=5,
            ports_registered=_PORTS,
            crosswalk_seed_hash=seed_hash(),
        )
