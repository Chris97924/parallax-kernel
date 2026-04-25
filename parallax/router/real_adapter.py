"""RealMemoryRouter real adapter for query/ingest/backfill routing.

Flag gate wiring remains at caller boundary (server route / CLI): this adapter
deliberately does NOT check ``is_router_enabled()`` inside its methods.
"""

from __future__ import annotations

import sqlite3
import types
from collections.abc import Mapping
from typing import Final

from parallax.obs.log import get_logger
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

_log = get_logger("parallax.router.real_adapter")

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
MEMORY_BODY_KEYS: Final[tuple[str, ...]] = (
    "body",
    "object_",
    "object",
    "payload_text",
    "text",
    "summary",
    "description",
)
MEMORY_TITLE_KEYS: Final[tuple[str, ...]] = ("title", "name")
CLAIM_OBJECT_KEYS: Final[tuple[str, ...]] = (
    "object_",
    "object",
    "body",
    "payload_text",
    "text",
    "summary",
)
CLAIM_SUBJECT_KEYS: Final[tuple[str, ...]] = ("subject", "entity", "name")
CLAIM_PREDICATE_KEYS: Final[tuple[str, ...]] = ("predicate", "event_type")


def _derive_body(hit: object) -> str:
    """US-D3-04: resolve canonical ``body`` for a retrieve hit.

    Single source of truth for alias precedence (PRD addendum, Sonnet Critic
    xcouncil Round 2): the same ``_first_non_empty`` helper used by
    ``RealMemoryRouter.ingest`` is used here for read-side projection.

    Read-side semantics:

    * **Missing alias** (legacy row without any recognized body key) — uses
      ``default=None`` so the helper returns ``None`` silently. We try the
      next source; if both sources lack the alias we fall back to the hit's
      title. **No warning** for this branch — legacy rows are an expected
      population.
    * **Malformed value** (type mismatch / lone surrogate) — ``ValueError``
      escapes despite ``default=None``. We catch it, record the reason,
      and emit a structured WARNING (``event=derive_body_fallback``) so
      operators can spot post-ingest data corruption.

    The fallback to ``title`` keeps the consumer contract (``body`` is
    always a ``str``) without aborting the whole response when a single
    hit is malformed.
    """
    from parallax.router.normalize import _first_non_empty

    kind = getattr(hit, "entity_kind", None)
    if kind == "memory":
        keys = MEMORY_BODY_KEYS
    elif kind == "claim":
        keys = CLAIM_OBJECT_KEYS
    else:
        return getattr(hit, "title", None) or ""

    fallback_reasons: list[str] = []
    for source_name, source in (
        ("full", getattr(hit, "full", None)),
        ("evidence", getattr(hit, "evidence", None)),
    ):
        if not source:
            continue
        # Codex P1: ``RetrievalHit.evidence`` is ``str | None`` per
        # parallax/retrieve.py; only Mapping sources can be alias-resolved.
        # Skip non-Mapping sources rather than letting _first_non_empty
        # raise AttributeError on payload.get(...) and turning a single
        # malformed/legacy hit into a hard query failure.
        if not isinstance(source, Mapping):
            continue
        try:
            result = _first_non_empty(source, keys, field=f"{kind}.body", default=None)
        except ValueError as exc:
            fallback_reasons.append(f"{source_name}: {exc}")
            continue
        if result is not None:
            return result
        # else: source dict has no recognized alias; not an error, try next.

    if fallback_reasons:
        _log.warning(
            "_derive_body fallback",
            extra={
                "event": "derive_body_fallback",
                "kind": kind,
                "entity_id": getattr(hit, "entity_id", None),
                "reasons": fallback_reasons,
            },
        )
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
                "created_at": (h.full or {}).get("created_at", ""),
                "source_id": (h.full or {}).get("source_id", ""),
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

        # Codex P2: ``IngestRequest`` is a frozen dataclass; the ``Literal``
        # type hint is a static-checker hint, not a runtime constraint. An
        # unvalidated caller (e.g. raw MCP request body) can still pass an
        # arbitrary string for ``kind``. Reject explicitly so we never
        # silently parse claim aliases on a non-claim payload.
        if request.kind not in ("memory", "claim"):
            raise ValueError(
                f"unsupported ingest kind {request.kind!r}; " f"expected 'memory' or 'claim'"
            )

        payload = request.payload

        if request.kind == "memory":
            body = _first_non_empty(payload, MEMORY_BODY_KEYS, field="memory.body")
            vault_path = _first_non_empty(payload, ("vault_path",), field="memory.vault_path")
            # Title is optional in the underlying schema. ``default=None``
            # makes "no recognized alias present" return None, but type and
            # surrogate errors still propagate (Codex review HIGH-4).
            title = _first_non_empty(payload, MEMORY_TITLE_KEYS, field="memory.title", default=None)
            persisted_id, deduped = ingest_memory_with_status(
                self._conn,
                user_id=request.user_id,
                title=title,
                summary=body,
                vault_path=vault_path,
                source_id=request.source_id,
            )
            return IngestResult(kind="memory", identifier=persisted_id, deduped=deduped)

        # request.kind == "claim" — guarded above against unsupported kinds.
        subject = _first_non_empty(payload, CLAIM_SUBJECT_KEYS, field="claim.subject")
        predicate = _first_non_empty(payload, CLAIM_PREDICATE_KEYS, field="claim.predicate")
        object_ = _first_non_empty(payload, CLAIM_OBJECT_KEYS, field="claim.object_")
        confidence = _coerce_optional_float(payload.get("confidence"), field="claim.confidence")
        # Codex P2: pass caller-supplied state through. Review-workflow
        # callers (e.g. extract layer with low-confidence "pending" claims)
        # would otherwise have their intent silently rewritten to the
        # "auto" default. ingest_claim_with_status validates against
        # CLAIM_TRANSITIONS and raises on unknown values.
        state = payload.get("state", "auto")
        persisted_id, deduped = ingest_claim_with_status(
            self._conn,
            user_id=request.user_id,
            subject=subject,
            predicate=predicate,
            object_=object_,
            source_id=request.source_id,
            confidence=confidence,
            state=state,
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
