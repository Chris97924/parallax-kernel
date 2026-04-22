"""Request / response contracts for the MEMORY_ROUTER routing layer (Lane D-1).

All dataclasses are frozen (immutable) and use tuple containers instead of
lists to stay hashable. RetrievalEvidence is re-exported from
parallax.retrieval.contracts — NOT redefined here.
"""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class IngestRequest:
    """Request to persist a memory or claim payload into the router store."""

    user_id: str
    kind: Literal["memory", "claim"]
    payload: dict[str, Any]
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
    """Router health and introspection snapshot."""

    ok: bool
    flag_enabled: bool
    query_type_count: int
    ports_registered: tuple[str, ...]
    crosswalk_seed_hash: str
