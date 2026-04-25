"""RealMemoryRouter real adapter for query/ingest/backfill routing.

Flag gate wiring remains at caller boundary (server route / CLI): this adapter
deliberately does NOT check ``is_router_enabled()`` inside its methods.
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

__all__ = [
    "RealMemoryRouter",
    "QUERY_DISPATCH",
    "MEMORY_BODY_KEYS",
    "MEMORY_TITLE_KEYS",
    "CLAIM_OBJECT_KEYS",
    "CLAIM_SUBJECT_KEYS",
    "CLAIM_PREDICATE_KEYS",
]

_DISPATCH: dict[QueryType, str] = {
    QueryType.RECENT_CONTEXT: "recent_context",
    QueryType.ARTIFACT_CONTEXT: "by_file",
    QueryType.ENTITY_PROFILE: "by_entity",
    QueryType.CHANGE_TRACE: "by_decision",
    QueryType.TEMPORAL_CONTEXT: "by_timeline",
}

# Alias-key tuples for IngestRequest.payload normalization (US-D3-01 / US-D3-04).
# Declared-order = canonical precedence. The Sonnet Critic xcouncil Round 2
# concern is addressed by routing both ingest-side normalization (this module)
# and read-side DTO body projection through the same _first_non_empty helper
# in parallax.router.normalize — single source of truth.
MEMORY_BODY_KEYS: tuple[str, ...] = (
    "body",
    "object_",
    "object",
    "payload_text",
    "text",
    "summary",
    "description",
)
MEMORY_TITLE_KEYS: tuple[str, ...] = ("title", "name")
CLAIM_OBJECT_KEYS: tuple[str, ...] = (
    "object_",
    "object",
    "body",
    "payload_text",
    "text",
    "summary",
)
CLAIM_SUBJECT_KEYS: tuple[str, ...] = ("subject", "entity", "name")
CLAIM_PREDICATE_KEYS: tuple[str, ...] = ("predicate", "event_type")


def _derive_body(hit: object) -> str:
    """US-D3-04: resolve canonical ``body`` for a retrieve hit.

    Single source of truth for alias precedence (PRD addendum, Sonnet Critic
    xcouncil Round 2): the same ``_first_non_empty`` helper used by
    ``RealMemoryRouter.ingest`` is used here for read-side projection.

    Read-side leniency: ``_first_non_empty`` raises ``ValueError`` on missing
    or malformed values, but a query that returned hits already passed
    persistence; falling back to the hit's title preserves consumer
    contract (``body`` is always a ``str``) without aborting the whole
    response if a single legacy row lacks a recognized body alias.
    """
    from parallax.router.normalize import _first_non_empty

    kind = getattr(hit, "entity_kind", None)
    if kind == "memory":
        keys = MEMORY_BODY_KEYS
    elif kind == "claim":
        keys = CLAIM_OBJECT_KEYS
    else:
        return getattr(hit, "title", None) or ""

    for source in (getattr(hit, "full", None), getattr(hit, "evidence", None)):
        if not source:
            continue
        try:
            return _first_non_empty(source, keys, field=f"{kind}.body")
        except ValueError:
            continue
    return getattr(hit, "title", None) or ""


QUERY_DISPATCH: Mapping[QueryType, str] = types.MappingProxyType(_DISPATCH)
# H-1 hardening: sever the mutable handle so no code outside this module can
# mutate _DISPATCH and sneak entries past the frozen MappingProxyType view.
del _DISPATCH

# H-1 (Lane D-2 security review): cap caller-supplied limit before forwarding
# into the SQL LIMIT parameter. Prevents OOM/DoS via request.limit=sys.maxsize.
_MAX_QUERY_LIMIT = 500

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

        if request.query_type is QueryType.TEMPORAL_CONTEXT and (
            request.since is None or request.until is None
        ):
            raise ValueError("TEMPORAL_CONTEXT requires since and until in QueryRequest")

        # Clamp caller-supplied limit to _MAX_QUERY_LIMIT (H-1 hardening).
        capped_limit = min(max(request.limit, 1), _MAX_QUERY_LIMIT)

        if request.query_type is QueryType.RECENT_CONTEXT:
            hits = _retrieve.recent_context(self._conn, user_id=request.user_id, limit=capped_limit)
        elif request.query_type is QueryType.ARTIFACT_CONTEXT:
            hits = _retrieve.by_file(
                self._conn, user_id=request.user_id, path=request.q, limit=capped_limit
            )
        elif request.query_type is QueryType.ENTITY_PROFILE:
            hits = _retrieve.by_entity(
                self._conn, user_id=request.user_id, subject=request.q, limit=capped_limit
            )
        elif request.query_type is QueryType.CHANGE_TRACE:
            hits = _retrieve.by_decision(self._conn, user_id=request.user_id, limit=capped_limit)
        else:  # TEMPORAL_CONTEXT — since/until already validated above
            hits = _retrieve.by_timeline(
                self._conn,
                user_id=request.user_id,
                since=request.since,  # type: ignore[arg-type]
                until=request.until,  # type: ignore[arg-type]
                limit=capped_limit,
            )

        retriever_name = QUERY_DISPATCH[request.query_type]

        hit_dicts = tuple(
            {
                "id": h.entity_id,
                "text": h.title,
                "body": _derive_body(h),
                "created_at": (h.full or {}).get("created_at", "") if h.full else "",
                "source_id": (h.full or {}).get("source_id", "") if h.full else "",
                "kind": h.entity_kind,
                "score": h.score,
                "evidence": h.evidence,
                "full": h.full if h.full is not None else h.evidence,
                "explain": h.explain,
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
        """Persist a memory or claim payload via parallax.ingest helpers.

        Field normalization uses the alias tuples ``MEMORY_BODY_KEYS`` /
        ``MEMORY_TITLE_KEYS`` / ``CLAIM_*_KEYS`` defined at module level. The
        same ``_first_non_empty`` helper is reused by ``query()`` for the
        canonical ``body`` DTO field, so ingest and read-side cannot
        diverge (PRD addendum, Sonnet Critic xcouncil Round 2).

        ``IngestResult.deduped`` is derived from the dedup status returned by
        ``ingest_*_with_status`` — no router-side content_hash pre-check
        (TOCTOU-prone).
        """
        # Method-local imports: keep parallax.router package import-discipline
        # green and avoid pulling parallax.ingest at adapter import time.
        from parallax.ingest import (
            ingest_claim_with_status,
            ingest_memory_with_status,
        )
        from parallax.router.normalize import (
            _coerce_optional_float,
            _first_non_empty,
        )

        payload = request.payload

        if request.kind == "memory":
            body = _first_non_empty(payload, MEMORY_BODY_KEYS, field="memory.body")
            vault_path = _first_non_empty(payload, ("vault_path",), field="memory.vault_path")
            # Title is optional in the underlying schema; absence is OK.
            title: str | None
            try:
                title = _first_non_empty(payload, MEMORY_TITLE_KEYS, field="memory.title")
            except ValueError:
                title = None
            persisted_id, deduped = ingest_memory_with_status(
                self._conn,
                user_id=request.user_id,
                title=title,
                summary=body,
                vault_path=vault_path,
                source_id=request.source_id,
            )
            return IngestResult(kind="memory", identifier=persisted_id, deduped=deduped)

        # request.kind == "claim" — Literal type guarantees no other branch.
        subject = _first_non_empty(payload, CLAIM_SUBJECT_KEYS, field="claim.subject")
        predicate = _first_non_empty(payload, CLAIM_PREDICATE_KEYS, field="claim.predicate")
        object_ = _first_non_empty(payload, CLAIM_OBJECT_KEYS, field="claim.object_")
        confidence = _coerce_optional_float(payload.get("confidence"), field="claim.confidence")
        persisted_id, deduped = ingest_claim_with_status(
            self._conn,
            user_id=request.user_id,
            subject=subject,
            predicate=predicate,
            object_=object_,
            source_id=request.source_id,
            confidence=confidence,
        )
        return IngestResult(kind="claim", identifier=persisted_id, deduped=deduped)

    def backfill(self, request: BackfillRequest) -> BackfillReport:
        """Delegate to :class:`BackfillRunner` for crosswalk-only writes.

        Method-local import keeps ``parallax.router`` package-level imports
        free of ``parallax.router.backfill`` side-effects, matching the
        same import discipline as ``query()``.
        """
        from parallax.router.backfill import BackfillRunner

        return BackfillRunner(self._conn).run(request)

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
