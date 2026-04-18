"""Bridge: provider-extracted RawClaims → parallax canonical claims.

The mapping encodes polarity into the predicate so opposite-polarity
claims with the same ``(entity, claim_text)`` hash to distinct rows::

    subject  = RawClaim.entity
    predicate = f"{RawClaim.claim_type}/{RawClaim.polarity:+d}"
    object_   = RawClaim.claim_text
    confidence = RawClaim.confidence
"""

from __future__ import annotations

import sqlite3

from parallax.extract.extractor import extract_claims
from parallax.extract.providers.base import Provider
from parallax.extract.types import RawClaim
from parallax.ingest import ingest_claim

__all__ = ["extract_and_ingest", "claim_predicate"]


def claim_predicate(raw: RawClaim) -> str:
    """Canonical predicate string used when persisting a RawClaim."""
    return f"{raw.claim_type}/{raw.polarity:+d}"


def extract_and_ingest(
    conn: sqlite3.Connection,
    text: str,
    *,
    provider: Provider,
    user_id: str,
    source_id: str | None = None,
) -> list[str]:
    """Extract claims via ``provider`` then UPSERT them via ``parallax.ingest_claim``.

    Returns the list of persisted ``claim_id`` values in extraction order.
    Empty text or empty provider output both return ``[]`` without opening
    a write transaction.
    """
    if not text or not text.strip():
        return []

    raw_claims = extract_claims(text, provider=provider)
    if not raw_claims:
        return []

    persisted: list[str] = []
    for raw in raw_claims:
        claim_id = ingest_claim(
            conn,
            user_id=user_id,
            subject=raw.entity,
            predicate=claim_predicate(raw),
            object_=raw.claim_text,
            source_id=source_id,
            confidence=raw.confidence,
        )
        persisted.append(claim_id)
    return persisted
