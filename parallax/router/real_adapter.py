"""RealMemoryRouter — Lane D-2 real adapter: query() dispatches to parallax.retrieve.*.

Lane D-3 deferred items (explicit, not silently hidden):
1. IngestPort.ingest real implementation — raises NotImplementedError in this lane.
2. Field normalization layer (memory.body / claim.object_ / event.payload_text
   canonical unification — Sonnet Critic's flagged tech debt).
3. ArbitrationDecision CLI view.
4. diff-audit human review gate.
5. Server-side flag wiring in parallax/server/routes/query.py — flag gate lives at
   the caller boundary; RealMemoryRouter deliberately does NOT check is_router_enabled()
   inside its methods (see class docstring).
"""

from __future__ import annotations

import sqlite3
import types
from collections.abc import Mapping

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import (
    BackfillReport,
    BackfillRequest,
    HealthReport,
    IngestRequest,
    IngestResult,
    QueryRequest,
)
from parallax.router.crosswalk_seed import seed_hash
from parallax.router.types import QueryType

__all__ = ["RealMemoryRouter", "QUERY_DISPATCH"]

_DISPATCH: dict[QueryType, str] = {
    QueryType.RECENT_CONTEXT: "recent_context",
    QueryType.ARTIFACT_CONTEXT: "by_file",
    QueryType.ENTITY_PROFILE: "by_entity",
    QueryType.CHANGE_TRACE: "by_decision",
    QueryType.TEMPORAL_CONTEXT: "by_timeline",
}

QUERY_DISPATCH: Mapping[QueryType, str] = types.MappingProxyType(_DISPATCH)
# H-1 hardening: sever the mutable handle so no code outside this module can
# mutate _DISPATCH and sneak entries past the frozen MappingProxyType view.
del _DISPATCH

_PORTS = ("QueryPort", "IngestPort", "InspectPort", "BackfillPort")
_D2_FREEZE_MSG = (
    "Lane D-2 freeze: RealMemoryRouter.{method} is intentionally unimplemented;"
    " ingest and full-backfill land in Lane D-3"
)


class RealMemoryRouter:
    """Real implementation of QueryPort / IngestPort / InspectPort / BackfillPort.

    Flag gate deliberate design decision: RealMemoryRouter does NOT check
    is_router_enabled() inside its methods. The flag gate lives at the caller
    boundary (server route / CLI), which Lane D-3 wires. This keeps the adapter
    trivially testable regardless of the MEMORY_ROUTER env var, and avoids
    repeating the flag check in every method.

    DB connection is injected via __init__ so the adapter is trivially testable
    with an in-memory SQLite DB — no global state, no module-level connect call.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        """Dispatch to the appropriate parallax.retrieve.* function via QUERY_DISPATCH.

        Method-local import of parallax.retrieve avoids triggering that module's
        module-level side-effects (metrics counters, sqlite_store import chain) at
        parallax.router import time, keeping the subprocess import-discipline test green.
        """
        # Method-local import: avoids bringing parallax.retrieve into sys.modules
        # when someone merely does `import parallax.router`. The import-discipline
        # test asserts "parallax.retrieve" not in sys.modules after importing the
        # router package — a top-level import here would break that invariant.
        from parallax import retrieve as _retrieve

        if (
            request.query_type is QueryType.TEMPORAL_CONTEXT
            and (request.since is None or request.until is None)
        ):
            raise ValueError(
                "TEMPORAL_CONTEXT requires since and until in QueryRequest"
            )

        retriever_name = QUERY_DISPATCH[request.query_type]

        if request.query_type is QueryType.RECENT_CONTEXT:
            hits = _retrieve.recent_context(
                self._conn, user_id=request.user_id, limit=request.limit
            )
        elif request.query_type is QueryType.ARTIFACT_CONTEXT:
            hits = _retrieve.by_file(
                self._conn, user_id=request.user_id, path=request.q, limit=request.limit
            )
        elif request.query_type is QueryType.ENTITY_PROFILE:
            hits = _retrieve.by_entity(
                self._conn, user_id=request.user_id, subject=request.q, limit=request.limit
            )
        elif request.query_type is QueryType.CHANGE_TRACE:
            hits = _retrieve.by_decision(
                self._conn, user_id=request.user_id, limit=request.limit
            )
        else:  # TEMPORAL_CONTEXT — since/until already validated above
            hits = _retrieve.by_timeline(
                self._conn,
                user_id=request.user_id,
                since=request.since,  # type: ignore[arg-type]
                until=request.until,  # type: ignore[arg-type]
                limit=request.limit,
            )

        hit_dicts = tuple(
            {
                "id": h.entity_id,
                "text": h.title,
                "created_at": (h.full or {}).get("created_at", "") if h.full else "",
                "source_id": getattr(h, "source_id", "") or "",
                "kind": h.entity_kind,
            }
            for h in hits
        )

        return RetrievalEvidence(
            hits=hit_dicts,
            stages=("real_adapter_dispatch",),
            notes=(
                f"query_type={request.query_type.value}",
                f"retriever={retriever_name}",
            ),
            sql_fragments=(),
            diversity_mode="none",
        )

    def ingest(self, request: IngestRequest) -> IngestResult:
        """Raise NotImplementedError — real ingest lands in Lane D-3."""
        raise NotImplementedError(_D2_FREEZE_MSG.format(method="ingest"))

    def backfill(self, request: BackfillRequest) -> BackfillReport:
        """Raise NotImplementedError — full backfill lands in Lane D-3."""
        raise NotImplementedError(_D2_FREEZE_MSG.format(method="backfill"))

    def health(self) -> HealthReport:
        """Return a real HealthReport.

        Late import of is_router_enabled() tracks runtime env changes
        (same pattern as MockMemoryRouter.health, M-3 hardening).
        """
        from parallax.router.config import is_router_enabled  # late import avoids circular

        return HealthReport(
            ok=True,
            flag_enabled=is_router_enabled(),
            query_type_count=len(QueryType),
            ports_registered=_PORTS,
            crosswalk_seed_hash=seed_hash(),
        )
