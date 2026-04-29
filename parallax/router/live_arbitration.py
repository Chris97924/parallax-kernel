"""Live cross-store arbitration contract (M3b Phase 2 — US-004-M3-T2.1).

This module is intentionally separate from
``parallax.router.contracts.ArbitrationDecision`` (which arbitrates
Crosswalk field-mapping state during backfill — semantics collide). The
contract here is the *runtime* arbitration verdict produced after a
DualReadRouter dispatch: for a given query, did Parallax or Aphelion
"win"?  This is the narrow, source-level rule table from PRD addendum
Q1 Option A (RECENT/ARTIFACT/CHANGE_TRACE/TEMPORAL → parallax,
ENTITY_PROFILE → aphelion).

Design pinning notes:

- ``LiveArbitrationDecision`` is a frozen dataclass; ``arbitrate`` is a
  pure function with no I/O and no side-effects.
- ``policy_version`` defaults to the current RC string (``v0.3.0-rc``).
  Old serialized lines may have been written before this field existed;
  on read, missing keys coerce to ``POLICY_VERSION_PRE_RC`` so the
  decoder is robust to historical data without ever raising.
- ``to_json_line`` uses ``json.dumps(..., sort_keys=True)`` for
  byte-deterministic output across runs.
- ``reason_code`` format: ``"source-level/{query_type}/{outcome}"``.
  Stable across calls with identical inputs (KISS — the spec did not
  mandate a richer schema).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Literal

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.types import QueryType

__all__ = [
    "POLICY_VERSION_PRE_RC",
    "POLICY_VERSION_DEFAULT",
    "LiveArbitrationDecision",
    "arbitrate",
]

# Sentinel value used when reading a serialized line that pre-dates the
# ``policy_version`` field.  Reader-side robustness — never written.
POLICY_VERSION_PRE_RC = "v0.0-pre-rc"

# Default policy version emitted by ``arbitrate`` for new decisions.
POLICY_VERSION_DEFAULT = "v0.3.0-rc"

# Source-level rule table: which store "owns" each QueryType when both
# sides return populated results (Q1 Option A).  Crosswalk-miss
# (secondary is None or has no hits) overrides this and always resolves
# to ``"fallback"``.
_QT_OWNERSHIP: dict[QueryType, Literal["parallax", "aphelion"]] = {
    QueryType.RECENT_CONTEXT: "parallax",
    QueryType.ARTIFACT_CONTEXT: "parallax",
    QueryType.CHANGE_TRACE: "parallax",
    QueryType.TEMPORAL_CONTEXT: "parallax",
    QueryType.ENTITY_PROFILE: "aphelion",
}


WinningSource = Literal["parallax", "aphelion", "tie", "fallback"]


@dataclass(frozen=True)
class LiveArbitrationDecision:
    """Immutable verdict for a single live cross-store query.

    Field semantics:
      - ``winning_source`` — "parallax" | "aphelion" | "tie" | "fallback".
      - ``tie_breaker_rule`` — short string identifying the rule applied
        (e.g. ``"source-level"`` for the Q1 Option A table).
      - ``conflict_event_id`` — when set, points at a row in the
        conflict-event log (Story 5).  None when no conflict event was
        emitted.
      - ``policy_version`` — version string of the rule table that
        produced this verdict.  Always non-null.
      - ``correlation_id`` — ties this decision to the originating
        DualReadRouter dispatch.
      - ``query_type`` — the QueryType that drove the rule selection.
      - ``reason_code`` — stable, machine-grep-able string. Format
        ``"source-level/{query_type}/{outcome}"``.
      - ``decided_at_us_utc`` — microsecond UTC timestamp at decision
        time. Set by ``arbitrate``; integer for byte-deterministic JSON.
    """

    winning_source: WinningSource
    tie_breaker_rule: str
    conflict_event_id: str | None
    policy_version: str
    correlation_id: str
    query_type: QueryType
    reason_code: str
    decided_at_us_utc: int

    @property
    def requires_manual_review(self) -> bool:
        """True when human inspection is warranted (tie or fallback)."""
        return self.winning_source in ("tie", "fallback")

    def to_json_line(self) -> str:
        """Serialize to a single JSON line with deterministic key order.

        Invariant: ``policy_version`` is always emitted as a non-null
        string. Callers that round-trip through this method get
        byte-equal output for byte-equal inputs.
        """
        payload = {
            "winning_source": self.winning_source,
            "tie_breaker_rule": self.tie_breaker_rule,
            "conflict_event_id": self.conflict_event_id,
            "policy_version": self.policy_version,
            "correlation_id": self.correlation_id,
            "query_type": self.query_type.value,
            "reason_code": self.reason_code,
            "decided_at_us_utc": self.decided_at_us_utc,
        }
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json_line(cls, line: str) -> LiveArbitrationDecision:
        """Decode a JSON line.

        Reader robustness: if the serialized dict is missing the
        ``policy_version`` key (pre-RC writers), coerce to
        :data:`POLICY_VERSION_PRE_RC` instead of raising.
        """
        data = json.loads(line)
        return cls(
            winning_source=data["winning_source"],
            tie_breaker_rule=data["tie_breaker_rule"],
            conflict_event_id=data.get("conflict_event_id"),
            policy_version=data.get("policy_version", POLICY_VERSION_PRE_RC),
            correlation_id=data["correlation_id"],
            query_type=QueryType(data["query_type"]),
            reason_code=data["reason_code"],
            decided_at_us_utc=int(data["decided_at_us_utc"]),
        )


def _is_empty(evidence: RetrievalEvidence | None) -> bool:
    """Return True if ``evidence`` is None or has no hits."""
    return evidence is None or len(evidence.hits) == 0


def arbitrate(
    primary: RetrievalEvidence,
    secondary: RetrievalEvidence | None,
    query_type: QueryType,
    correlation_id: str,
) -> LiveArbitrationDecision:
    """Apply the Q1 Option A source-level rule table.

    Rules (in order):
      1. If ``secondary`` is None or empty → ``winning_source="fallback"``
         (crosswalk-miss). This applies regardless of ``query_type``.
      2. Else, if ``primary`` is empty → still ``"fallback"`` (we cannot
         claim a parallax win without parallax data).
      3. Else, lookup ``_QT_OWNERSHIP[query_type]`` and return that.

    Pure function: no I/O, no logging, no global state.  ``arbitrate``
    constructs a fresh ``LiveArbitrationDecision`` and returns it.
    """
    if _is_empty(secondary) or _is_empty(primary):
        winning_source: WinningSource = "fallback"
    else:
        winning_source = _QT_OWNERSHIP[query_type]

    reason_code = f"source-level/{query_type.value}/{winning_source}"
    decided_at_us_utc = time.time_ns() // 1_000

    return LiveArbitrationDecision(
        winning_source=winning_source,
        tie_breaker_rule="source-level",
        conflict_event_id=None,
        policy_version=POLICY_VERSION_DEFAULT,
        correlation_id=correlation_id,
        query_type=query_type,
        reason_code=reason_code,
        decided_at_us_utc=decided_at_us_utc,
    )
