"""Shared DTOs for the extract layer.

``RawClaim`` is the hand-off shape between a ``Provider`` and the ingest
bridge. Frozen dataclass so downstream code can treat the value as a key
in dedup maps without surprises.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["RawClaim"]


@dataclass(frozen=True)
class RawClaim:
    entity: str
    claim_text: str
    polarity: int
    confidence: float
    claim_type: str
    evidence: str
