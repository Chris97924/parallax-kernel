"""Tests for DualReadResult contract (M3-T1.2, US-011)."""

from __future__ import annotations

import dataclasses

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import DualReadResult

_EVIDENCE = RetrievalEvidence(hits=(), stages=("test",))


def _make_result(**kwargs) -> DualReadResult:
    defaults = dict(
        outcome="match",
        primary=_EVIDENCE,
        secondary=None,
        correlation_id="cid-1",
        latency_primary_ms=5.0,
        latency_secondary_ms=None,
        aphelion_unreachable_reason=None,
    )
    defaults.update(kwargs)
    return DualReadResult(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_dual_read_result_frozen() -> None:
    r = _make_result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.outcome = "diverge"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# All 5 outcome values are constructable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    ["match", "diverge", "primary_only", "aphelion_unreachable", "skipped"],
)
def test_all_outcome_values_constructable(outcome: str) -> None:
    r = _make_result(outcome=outcome)
    assert r.outcome == outcome


# ---------------------------------------------------------------------------
# secondary is None on skipped / aphelion_unreachable
# ---------------------------------------------------------------------------


def test_secondary_none_on_skipped() -> None:
    r = _make_result(outcome="skipped", secondary=None)
    assert r.secondary is None


def test_secondary_none_on_aphelion_unreachable() -> None:
    r = _make_result(
        outcome="aphelion_unreachable",
        secondary=None,
        aphelion_unreachable_reason="timeout",
    )
    assert r.secondary is None
    assert r.aphelion_unreachable_reason == "timeout"


def test_secondary_present_on_match() -> None:
    r = _make_result(outcome="match", secondary=_EVIDENCE, latency_secondary_ms=3.0)
    assert r.secondary is _EVIDENCE
    assert r.latency_secondary_ms == 3.0


# ---------------------------------------------------------------------------
# primary is always set
# ---------------------------------------------------------------------------


def test_primary_always_set() -> None:
    r = _make_result(outcome="primary_only", secondary=None)
    assert r.primary is _EVIDENCE


# ---------------------------------------------------------------------------
# Fields are accessible
# ---------------------------------------------------------------------------


def test_fields() -> None:
    names = [f.name for f in dataclasses.fields(DualReadResult)]
    assert names == [
        "outcome",
        "primary",
        "secondary",
        "correlation_id",
        "latency_primary_ms",
        "latency_secondary_ms",
        "aphelion_unreachable_reason",
    ]


# ---------------------------------------------------------------------------
# DualReadResult is re-exported from contracts.__all__
# ---------------------------------------------------------------------------


def test_dual_read_result_in_all() -> None:
    from parallax.router import contracts

    assert "DualReadResult" in contracts.__all__
