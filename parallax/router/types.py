"""QueryType enum — 5-value closed set for the MEMORY_ROUTER routing layer.

This module is the foundation of the router package (Lane D-1). It must be
importable without touching parallax.retrieval or parallax.server.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = ["QueryType", "MappingState", "FieldCandidate", "DataQualityFlag"]


class QueryType(StrEnum):
    """Five-value closed set for MEMORY_ROUTER routing (Lane D-1)."""

    # RECENT_CONTEXT = near-term conversation + multi-session continuity
    RECENT_CONTEXT = "recent_context"
    # ARTIFACT_CONTEXT = file / path / artifact memory
    ARTIFACT_CONTEXT = "artifact_context"
    # ENTITY_PROFILE = entity profile (user_fact / preference / named entity)
    ENTITY_PROFILE = "entity_profile"
    # CHANGE_TRACE = decisions + bug fixes (change history)
    CHANGE_TRACE = "change_trace"
    # TEMPORAL_CONTEXT = when / before / after time-window queries
    TEMPORAL_CONTEXT = "temporal_context"


class MappingState(StrEnum):
    """State of a field mapping during arbitration."""

    MAPPED = "mapped"
    UNMAPPED = "unmapped"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class FieldCandidate:
    """A single candidate field value from one data source."""

    source: str
    field_name: str
    value: Any
    confidence: float


class DataQualityFlag(StrEnum):
    """Data-maturity tag emitted alongside conflict events (M3b — US-005).

    The DUAL_READ rollout has a 30-day cold-start window during which the
    crosswalk + arbitration signal is too sparse to feed the golden-dataset
    audit pipeline. Conflict-event writers stamp every row with one of three
    values so downstream readers can filter out "noisy" early data without
    losing the row entirely.

    Values:
      - ``COLD_START``      — < 14 days since DUAL_READ enabled. Default; do
                              not feed into golden-dataset audits.
      - ``CORPUS_IMMATURE`` — 14-30 days. Partial signal; opt-in only.
      - ``NORMAL``          — >= 30 days. Golden-dataset eligible.

    The conflict-event writer defaults to ``COLD_START`` until the CLI in
    Story 6 wires explicit corpus-age detection.
    """

    COLD_START = "cold_start"
    CORPUS_IMMATURE = "corpus_immature"
    NORMAL = "normal"
