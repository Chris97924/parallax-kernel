"""Request / response contracts for the MEMORY_ROUTER routing layer (Lane D-1).

All dataclasses are frozen (immutable) and use tuple containers instead of
lists to stay hashable. RetrievalEvidence is re-exported from
parallax.retrieval.contracts — NOT redefined here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.types import FieldCandidate, MappingState, QueryType

__all__ = [
    "QueryRequest",
    "IngestRequest",
    "IngestResult",
    "BackfillRequest",
    "BackfillReport",
    "HealthReport",
    "ArbitrationDecision",
    "RetrievalEvidence",
    "DualReadResult",
]


@dataclass(frozen=True)
class QueryRequest:
    """Typed query request routed through the MEMORY_ROUTER."""

    query_type: QueryType
    user_id: str
    q: str = ""
    limit: int = 10
    since: str | None = None
    until: str | None = None
    level: int = 1
    # ADR-007: payload-level sub-dispatch for CHANGE_TRACE.
    # Callers set params={"legacy_kind": "bug"} to route to by_bug_fix;
    # default None routes to by_decision. Additive, backward-compatible.
    params: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class IngestRequest:
    """Request to persist a memory or claim payload into the router store.

    payload is typed as Mapping (not dict) to signal read-only intent. The
    frozen=True flag only freezes field reassignment; the underlying dict
    object is still mutable by anyone holding the original reference.
    SF1 hardening from 2-agent review.
    """

    user_id: str
    kind: Literal["memory", "claim"]
    payload: Mapping[str, Any]
    source_id: str | None = None


@dataclass(frozen=True)
class IngestResult:
    """Result of a successful ingest operation."""

    kind: Literal["memory", "claim"]
    identifier: str
    deduped: bool


@dataclass(frozen=True)
class BackfillRequest:
    """Request to run a Crosswalk-driven backfill."""

    user_id: str
    crosswalk_version: str
    dry_run: bool = True
    scope: Literal["all", "sample"] = "sample"


@dataclass(frozen=True)
class ArbitrationDecision:
    """Result of arbitrating a single field across multiple data sources."""

    canonical_field: str
    state: MappingState
    selected: FieldCandidate | None
    candidates: tuple[FieldCandidate, ...]
    reason_code: str
    reason: str
    confidence: float
    requires_manual_review: bool

    def to_json_line(self) -> str:
        """Serialize to a single JSON line with deterministic key order."""
        return json.dumps(
            {
                "canonical_field": self.canonical_field,
                "state": self.state.value,
                "selected": (
                    {
                        "source": self.selected.source,
                        "field_name": self.selected.field_name,
                        "value": self.selected.value,
                        "confidence": self.selected.confidence,
                    }
                    if self.selected is not None
                    else None
                ),
                "candidates": [
                    {
                        "source": c.source,
                        "field_name": c.field_name,
                        "value": c.value,
                        "confidence": c.confidence,
                    }
                    for c in self.candidates
                ],
                "reason_code": self.reason_code,
                "reason": self.reason,
                "confidence": self.confidence,
                "requires_manual_review": self.requires_manual_review,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class BackfillReport:
    """Summary report from a backfill run."""

    rows_examined: int
    rows_mapped: int
    rows_unmapped: int
    rows_conflict: int
    writes_performed: int
    arbitrations: tuple[ArbitrationDecision, ...]


@dataclass(frozen=True)
class HealthReport:
    """Router health and introspection snapshot.

    WARNING (Lane D-2): the fields crosswalk_seed_hash, ports_registered, and
    flag_enabled are internal-topology recon assets. Before wiring this type
    into any unauthenticated HTTP endpoint (e.g. /inspect/health), either
    gate the endpoint behind auth middleware or emit a stripped public
    variant (e.g. {"ok": bool}) for unauthenticated callers.
    H-2 hardening note from 2-agent review.
    """

    ok: bool
    flag_enabled: bool
    query_type_count: int
    ports_registered: tuple[str, ...]
    crosswalk_seed_hash: str


# ---------------------------------------------------------------------------
# M3-T1.2 — DualReadResult (US-011)
# ---------------------------------------------------------------------------

# Five outcome labels mirroring DualReadOutcome from discrepancy_live.py.
# Defined here as a plain Literal to avoid a circular import with discrepancy_live.
_DualReadOutcome = Literal["match", "diverge", "primary_only", "aphelion_unreachable", "skipped"]


@dataclass(frozen=True)
class DualReadResult:
    """Result of a dual-read query dispatched by DualReadRouter (M3-T1.2).

    ``primary`` is always set — DualReadRouter is fail-closed to the canonical
    result on every failure path.  ``secondary`` is None when the Aphelion side
    was not called (``outcome="skipped"``) or when it failed
    (``outcome="aphelion_unreachable"`` / ``outcome="primary_only"``).

    outcome values:
      - ``"match"``              — both sides returned; hits equal under _hits_equal.
      - ``"diverge"``            — both sides returned; hits differ.
      - ``"primary_only"``       — secondary raised an unexpected exception.
      - ``"aphelion_unreachable"`` — secondary raised AphelionUnreachableError or timed out.
      - ``"skipped"``            — flag off OR Q5 CHANGE_TRACE.legacy_kind=bug.
    """

    outcome: _DualReadOutcome
    primary: RetrievalEvidence
    secondary: RetrievalEvidence | None
    correlation_id: str
    latency_primary_ms: float
    latency_secondary_ms: float | None
    aphelion_unreachable_reason: str | None
