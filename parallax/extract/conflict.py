"""DB-native conflict detection — no vault / no yaml.

A *conflict* is an existing claim that (a) names the same entity and
(b) carries the opposite polarity and (c) is textually similar above a
fixed Jaccard threshold. This layer only reads from the canonical SQLite
store; applying edges / writing contradiction metadata is out of scope.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from parallax.extract.types import RawClaim
from parallax.sqlite_store import query

__all__ = [
    "Conflict",
    "CONFLICT_THRESHOLD",
    "token_overlap",
    "detect_conflicts",
]

CONFLICT_THRESHOLD = 0.4

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Conflict:
    new_claim: RawClaim
    existing_claim_id: str
    existing_claim_text: str
    similarity: float


def token_overlap(a: str, b: str) -> float:
    """Jaccard similarity on case-folded word tokens. Empty side → 0.0."""
    sa = set(_TOKEN_RE.findall(a.lower()))
    sb = set(_TOKEN_RE.findall(b.lower()))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _opposite_polarity_predicates(new_polarity: int, claim_type: str) -> list[str]:
    """All predicates that constitute an opposite-polarity match.

    Mirrors ``claim_predicate``'s ``{claim_type}/{polarity:+d}`` format so we
    compare the same namespace we wrote on ingest. Neutral (0) conflicts
    with any signed polarity; ±1 conflicts with ∓1 and with 0 (matches the
    a2a conflict_detector semantics: 0 vs ±1 is conflict-eligible).
    """
    if new_polarity == 0:
        candidates = [1, -1]
    elif new_polarity > 0:
        candidates = [-1, 0]
    else:
        candidates = [1, 0]
    return [f"{claim_type}/{p:+d}" for p in candidates]


def detect_conflicts(
    conn: sqlite3.Connection,
    new_claim: RawClaim,
    *,
    user_id: str,
) -> list[Conflict]:
    """Return existing claims that contradict ``new_claim`` above the threshold."""
    predicates = _opposite_polarity_predicates(new_claim.polarity, new_claim.claim_type)
    placeholders = ",".join("?" * len(predicates))
    rows = query(
        conn,
        f"""
        SELECT claim_id, object
          FROM claims
         WHERE user_id = ?
           AND subject = ?
           AND predicate IN ({placeholders})
        """,
        (user_id, new_claim.entity, *predicates),
    )

    conflicts: list[Conflict] = []
    for row in rows:
        existing_text = row["object"] or ""
        similarity = token_overlap(existing_text, new_claim.claim_text)
        if similarity > CONFLICT_THRESHOLD:
            conflicts.append(
                Conflict(
                    new_claim=new_claim,
                    existing_claim_id=row["claim_id"],
                    existing_claim_text=existing_text,
                    similarity=round(similarity, 4),
                )
            )
    return conflicts
