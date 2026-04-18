"""Tests for parallax.extract.conflict."""

from __future__ import annotations

import sqlite3

from parallax.extract import RawClaim, extract_and_ingest
from parallax.extract.conflict import (
    CONFLICT_THRESHOLD,
    detect_conflicts,
    token_overlap,
)
from parallax.extract.providers.mock import MockProvider


def _raw(
    entity: str = "bitcoin",
    text: str = "it is volatile",
    polarity: int = -1,
    ctype: str = "risk",
) -> RawClaim:
    return RawClaim(
        entity=entity,
        claim_text=text,
        polarity=polarity,
        confidence=0.9,
        claim_type=ctype,
        evidence="",
    )


class TestTokenOverlap:
    def test_identical(self) -> None:
        assert token_overlap("hello world", "hello world") == 1.0

    def test_empty_sides(self) -> None:
        assert token_overlap("", "abc") == 0.0
        assert token_overlap("abc", "") == 0.0
        assert token_overlap("", "") == 0.0

    def test_half_overlap(self) -> None:
        # {a,b} vs {b,c} => |∩|=1 |∪|=3 => 1/3
        assert abs(token_overlap("a b", "b c") - (1 / 3)) < 1e-9

    def test_case_insensitive(self) -> None:
        assert token_overlap("Hello World", "hello world") == 1.0


class TestDetectConflicts:
    def test_no_existing_claims(self, conn: sqlite3.Connection) -> None:
        assert detect_conflicts(conn, _raw(), user_id="chris") == []

    def test_same_polarity_not_a_conflict(self, conn: sqlite3.Connection) -> None:
        extract_and_ingest(
            conn,
            "t",
            provider=MockProvider(
                claims=[_raw(text="bitcoin is volatile asset", polarity=-1, ctype="risk")]
            ),
            user_id="chris",
        )
        new = _raw(text="bitcoin is volatile asset", polarity=-1, ctype="risk")
        assert detect_conflicts(conn, new, user_id="chris") == []

    def test_opposite_polarity_similar_flags_conflict(
        self, conn: sqlite3.Connection
    ) -> None:
        extract_and_ingest(
            conn,
            "t",
            provider=MockProvider(
                claims=[_raw(text="bitcoin is a stable store of value", polarity=1, ctype="risk")]
            ),
            user_id="chris",
        )
        new = _raw(text="bitcoin is a volatile store of value", polarity=-1, ctype="risk")
        conflicts = detect_conflicts(conn, new, user_id="chris")
        assert len(conflicts) == 1
        assert conflicts[0].similarity > CONFLICT_THRESHOLD

    def test_opposite_polarity_dissimilar_no_conflict(
        self, conn: sqlite3.Connection
    ) -> None:
        extract_and_ingest(
            conn,
            "t",
            provider=MockProvider(
                claims=[_raw(text="totally unrelated alpha beta gamma", polarity=1, ctype="risk")]
            ),
            user_id="chris",
        )
        new = _raw(text="xyz qqq mmm nnn different", polarity=-1, ctype="risk")
        assert detect_conflicts(conn, new, user_id="chris") == []

    def test_different_user_not_considered(self, conn: sqlite3.Connection) -> None:
        extract_and_ingest(
            conn,
            "t",
            provider=MockProvider(
                claims=[_raw(text="alpha beta gamma delta", polarity=1, ctype="risk")]
            ),
            user_id="alice",
        )
        new = _raw(text="alpha beta gamma delta", polarity=-1, ctype="risk")
        assert detect_conflicts(conn, new, user_id="chris") == []
