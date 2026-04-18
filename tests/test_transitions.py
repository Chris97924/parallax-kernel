"""Tests for parallax.transitions — state matrices + is_allowed_transition."""

from __future__ import annotations

import pytest

from parallax.transitions import (
    CLAIM_TRANSITIONS,
    DECISION_TRANSITIONS,
    MEMORY_TRANSITIONS,
    SOURCE_TRANSITIONS,
    is_allowed_transition,
)


class TestMemoryMatrix:
    def test_draft_to_active_allowed(self) -> None:
        assert is_allowed_transition("memory", "draft", "active") is True

    def test_draft_to_archived_allowed(self) -> None:
        assert is_allowed_transition("memory", "draft", "archived") is True

    def test_active_to_archived_allowed(self) -> None:
        assert is_allowed_transition("memory", "active", "archived") is True

    def test_active_to_draft_disallowed(self) -> None:
        assert is_allowed_transition("memory", "active", "draft") is False

    def test_archived_is_terminal(self) -> None:
        assert MEMORY_TRANSITIONS["archived"] == frozenset()
        assert is_allowed_transition("memory", "archived", "active") is False


class TestClaimMatrix:
    def test_auto_to_pending_allowed(self) -> None:
        assert is_allowed_transition("claim", "auto", "pending") is True

    def test_auto_to_confirmed_allowed(self) -> None:
        assert is_allowed_transition("claim", "auto", "confirmed") is True

    def test_pending_to_confirmed_allowed(self) -> None:
        assert is_allowed_transition("claim", "pending", "confirmed") is True

    def test_pending_to_auto_disallowed(self) -> None:
        assert is_allowed_transition("claim", "pending", "auto") is False

    def test_confirmed_to_rejected_allowed(self) -> None:
        assert is_allowed_transition("claim", "confirmed", "rejected") is True

    def test_rejected_is_terminal(self) -> None:
        assert CLAIM_TRANSITIONS["rejected"] == frozenset()
        assert is_allowed_transition("claim", "rejected", "confirmed") is False


class TestSourceMatrix:
    def test_ingested_to_parsed_allowed(self) -> None:
        assert is_allowed_transition("source", "ingested", "parsed") is True

    def test_archived_is_terminal(self) -> None:
        assert SOURCE_TRANSITIONS["archived"] == frozenset()


class TestDecisionMatrix:
    def test_proposed_to_approved_allowed(self) -> None:
        assert is_allowed_transition("decision", "proposed", "approved") is True

    def test_approved_to_applied_allowed(self) -> None:
        assert is_allowed_transition("decision", "approved", "applied") is True

    def test_revoked_is_terminal(self) -> None:
        assert DECISION_TRANSITIONS["revoked"] == frozenset()


class TestErrorPaths:
    def test_unknown_entity_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown entity"):
            is_allowed_transition("rocket", "lit", "exploded")

    def test_unknown_from_state_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown memory from_state"):
            is_allowed_transition("memory", "limbo", "active")

    def test_unknown_to_state_returns_false(self) -> None:
        assert is_allowed_transition("memory", "draft", "limbo") is False
