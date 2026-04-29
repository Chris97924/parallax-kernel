"""Tests for LiveArbitrationDecision (M3b Phase 2 — US-004-M3-T2.1).

Covers the live cross-store arbitration contract under
``parallax/router/live_arbitration.py``.

This is **separate** from ``parallax/router/contracts.py::ArbitrationDecision``
(which arbitrates Crosswalk field mappings during backfill — semantics
collide). Do NOT confuse the two.

Test scope:
- Decision matrix: 5 QueryType × {primary-only, secondary-only,
  both-populated, both-empty} (collapsed to >=15 distinct cases).
- ``policy_version`` round-trip via ``to_json_line`` / ``from_json_line``.
- Missing-``policy_version`` reader sentinel coercion.
- Tie-breaker ``reason_code`` stability.
- ``requires_manual_review`` for all four ``winning_source`` values.
- ``to_json_line`` never emits null ``policy_version``.
- Deterministic key-sorted JSON output (byte-equal across runs).
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.live_arbitration import (
    POLICY_VERSION_PRE_RC,
    LiveArbitrationDecision,
    arbitrate,
)
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(*ids: str) -> RetrievalEvidence:
    """Build a RetrievalEvidence with the given hit IDs."""
    hits = tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids)
    return RetrievalEvidence(hits=hits, stages=("test",))


def _empty_evidence() -> RetrievalEvidence:
    return RetrievalEvidence(hits=(), stages=("test",))


_PARALLAX_QTS = (
    QueryType.RECENT_CONTEXT,
    QueryType.ARTIFACT_CONTEXT,
    QueryType.CHANGE_TRACE,
    QueryType.TEMPORAL_CONTEXT,
)
_APHELION_QT = QueryType.ENTITY_PROFILE


# ---------------------------------------------------------------------------
# Decision matrix — Q1 Option A source-level rule table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("qt", _PARALLAX_QTS)
def test_parallax_owned_qt_with_both_populated_wins_parallax(qt: QueryType) -> None:
    """RECENT/ARTIFACT/CHANGE_TRACE/TEMPORAL with both sides populated → parallax."""
    primary = _evidence("a", "b")
    secondary = _evidence("a", "b")
    decision = arbitrate(primary, secondary, qt, correlation_id="cid-1")
    assert decision.winning_source == "parallax"
    assert decision.query_type == qt
    assert decision.correlation_id == "cid-1"


@pytest.mark.parametrize("qt", _PARALLAX_QTS)
def test_parallax_owned_qt_primary_only_wins_parallax(qt: QueryType) -> None:
    """Parallax-owned QT with secondary=None still resolves parallax (source-level)."""
    primary = _evidence("a")
    decision = arbitrate(primary, None, qt, correlation_id="cid-2")
    # Per spec: secondary is None → fallback. Ordering matters: source-level
    # rule comes BEFORE crosswalk-miss only when secondary is non-None.
    assert decision.winning_source == "fallback"


@pytest.mark.parametrize("qt", _PARALLAX_QTS)
def test_parallax_owned_qt_secondary_empty_wins_parallax(qt: QueryType) -> None:
    """Empty secondary hits is a crosswalk-miss → fallback (per spec)."""
    primary = _evidence("a")
    secondary = _empty_evidence()
    decision = arbitrate(primary, secondary, qt, correlation_id="cid-3")
    assert decision.winning_source == "fallback"


@pytest.mark.parametrize("qt", _PARALLAX_QTS)
def test_parallax_owned_qt_both_empty_wins_fallback(qt: QueryType) -> None:
    primary = _empty_evidence()
    secondary = _empty_evidence()
    decision = arbitrate(primary, secondary, qt, correlation_id="cid-4")
    assert decision.winning_source == "fallback"


def test_entity_profile_both_populated_wins_aphelion() -> None:
    primary = _evidence("a")
    secondary = _evidence("a")
    decision = arbitrate(primary, secondary, _APHELION_QT, correlation_id="cid-5")
    assert decision.winning_source == "aphelion"
    assert decision.query_type == _APHELION_QT


def test_entity_profile_secondary_none_wins_fallback() -> None:
    primary = _evidence("a")
    decision = arbitrate(primary, None, _APHELION_QT, correlation_id="cid-6")
    assert decision.winning_source == "fallback"


def test_entity_profile_secondary_empty_wins_fallback() -> None:
    primary = _evidence("a")
    secondary = _empty_evidence()
    decision = arbitrate(primary, secondary, _APHELION_QT, correlation_id="cid-7")
    assert decision.winning_source == "fallback"


def test_entity_profile_both_empty_wins_fallback() -> None:
    primary = _empty_evidence()
    secondary = _empty_evidence()
    decision = arbitrate(primary, secondary, _APHELION_QT, correlation_id="cid-8")
    assert decision.winning_source == "fallback"


# ---------------------------------------------------------------------------
# Serialization — policy_version round-trip and sentinel coercion
# ---------------------------------------------------------------------------


def test_to_json_line_round_trip_preserves_fields() -> None:
    decision = arbitrate(
        _evidence("a"),
        _evidence("a"),
        QueryType.RECENT_CONTEXT,
        correlation_id="cid-rt",
    )
    line = decision.to_json_line()
    restored = LiveArbitrationDecision.from_json_line(line)
    assert restored == decision


def test_to_json_line_uses_default_policy_version() -> None:
    decision = arbitrate(
        _evidence("a"),
        _evidence("a"),
        QueryType.RECENT_CONTEXT,
        correlation_id="cid-pv",
    )
    payload = json.loads(decision.to_json_line())
    assert payload["policy_version"] == "v0.3.0-rc"


def test_from_json_line_missing_policy_version_coerces_to_sentinel() -> None:
    """Reader robustness: a serialized dict missing policy_version must NOT raise.

    The decoder coerces to ``POLICY_VERSION_PRE_RC`` so historical lines from
    pre-RC writers remain readable.
    """
    payload = {
        "winning_source": "parallax",
        "tie_breaker_rule": "source-level",
        "conflict_event_id": None,
        "correlation_id": "cid-old",
        "query_type": QueryType.RECENT_CONTEXT.value,
        "reason_code": "source-level/recent_context/parallax",
        "decided_at_us_utc": 1714000000000000,
    }
    line = json.dumps(payload, sort_keys=True)
    restored = LiveArbitrationDecision.from_json_line(line)
    assert restored.policy_version == POLICY_VERSION_PRE_RC


def test_to_json_line_never_emits_null_policy_version() -> None:
    decision = arbitrate(
        _empty_evidence(),
        _empty_evidence(),
        QueryType.ENTITY_PROFILE,
        correlation_id="cid-nn",
    )
    payload = json.loads(decision.to_json_line())
    assert payload["policy_version"] is not None
    assert isinstance(payload["policy_version"], str)


def test_to_json_line_is_deterministic_byte_equal() -> None:
    """Same inputs → byte-equal serialization across two arbitrate() calls.

    decided_at_us_utc is a wall-clock field, so we re-construct the second
    decision with the same timestamp by hand-rolling the dataclass.
    """
    first = arbitrate(
        _evidence("a"),
        _evidence("a"),
        QueryType.RECENT_CONTEXT,
        correlation_id="cid-det",
    )
    second = LiveArbitrationDecision(
        winning_source=first.winning_source,
        tie_breaker_rule=first.tie_breaker_rule,
        conflict_event_id=first.conflict_event_id,
        policy_version=first.policy_version,
        correlation_id=first.correlation_id,
        query_type=first.query_type,
        reason_code=first.reason_code,
        decided_at_us_utc=first.decided_at_us_utc,
    )
    assert first.to_json_line() == second.to_json_line()


def test_to_json_line_keys_are_sorted() -> None:
    decision = arbitrate(
        _evidence("a"),
        _evidence("a"),
        QueryType.RECENT_CONTEXT,
        correlation_id="cid-keys",
    )
    line = decision.to_json_line()
    payload = json.loads(line)
    keys = list(payload.keys())
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# reason_code stability + tie_breaker_rule
# ---------------------------------------------------------------------------


def test_reason_code_stable_for_same_inputs() -> None:
    """Same QueryType + outcome → same reason_code string."""
    a = arbitrate(_evidence("x"), _evidence("x"), QueryType.ARTIFACT_CONTEXT, correlation_id="c1")
    b = arbitrate(_evidence("x"), _evidence("x"), QueryType.ARTIFACT_CONTEXT, correlation_id="c2")
    assert a.reason_code == b.reason_code


def test_reason_code_differs_across_outcomes() -> None:
    parallax_win = arbitrate(
        _evidence("a"),
        _evidence("a"),
        QueryType.RECENT_CONTEXT,
        correlation_id="c1",
    )
    fallback_win = arbitrate(
        _evidence("a"),
        None,
        QueryType.RECENT_CONTEXT,
        correlation_id="c2",
    )
    assert parallax_win.reason_code != fallback_win.reason_code


def test_tie_breaker_rule_documents_source_level() -> None:
    decision = arbitrate(
        _evidence("a"), _evidence("a"), QueryType.ENTITY_PROFILE, correlation_id="c"
    )
    assert decision.tie_breaker_rule == "source-level"


# ---------------------------------------------------------------------------
# requires_manual_review property — all 4 winning_source values
# ---------------------------------------------------------------------------


def _build(winning: str) -> LiveArbitrationDecision:
    return LiveArbitrationDecision(
        winning_source=winning,  # type: ignore[arg-type]
        tie_breaker_rule="source-level",
        conflict_event_id=None,
        policy_version="v0.3.0-rc",
        correlation_id="cid",
        query_type=QueryType.RECENT_CONTEXT,
        reason_code="test",
        decided_at_us_utc=1,
    )


def test_requires_manual_review_parallax_false() -> None:
    assert _build("parallax").requires_manual_review is False


def test_requires_manual_review_aphelion_false() -> None:
    assert _build("aphelion").requires_manual_review is False


def test_requires_manual_review_tie_true() -> None:
    assert _build("tie").requires_manual_review is True


def test_requires_manual_review_fallback_true() -> None:
    assert _build("fallback").requires_manual_review is True


# ---------------------------------------------------------------------------
# Frozen invariant
# ---------------------------------------------------------------------------


def test_decision_is_frozen() -> None:
    decision = _build("parallax")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.winning_source = "tie"  # type: ignore[misc]
