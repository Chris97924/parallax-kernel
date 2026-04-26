"""Lane C US-006 — Shadow Mode interceptor TDD coverage.

The interceptor wraps a canonical router and computes a shadow decision
without mutating the canonical result. These tests cover the public
contract: bypass logic, log schema, divergence capture, latency, and
correlation-id propagation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest
from parallax.router.shadow import ShadowDecisionLog, ShadowInterceptor
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeRouter:
    """Stub router that returns whatever evidence the test sets up.

    Records calls so tests can assert query() was invoked exactly once
    (or not at all in bypass cases).
    """

    evidence: RetrievalEvidence
    calls: list[QueryRequest]

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        self.calls.append(request)
        return self.evidence


def _make_evidence(*hit_ids: str) -> RetrievalEvidence:
    return RetrievalEvidence(
        hits=tuple(
            {"id": hid, "kind": "memory", "score": 0.9, "body": "", "text": hid} for hid in hit_ids
        ),
        stages=("test_stage",),
    )


def _make_request(user_id: str = "user1") -> QueryRequest:
    return QueryRequest(query_type=QueryType.RECENT_CONTEXT, user_id=user_id, limit=5)


@pytest.fixture()
def shadow_log_dir(tmp_path, monkeypatch) -> Any:
    """Redirect SHADOW_LOG_DIR to tmp_path so tests don't touch real disk."""
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path))
    return tmp_path


def _read_log_lines(log_dir) -> list[dict]:
    """Concatenate every JSONL line from every file under shadow_log_dir."""
    lines: list[dict] = []
    for path in sorted(log_dir.glob("shadow-decisions-*.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                lines.append(json.loads(raw))
    return lines


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


def test_decision_log_has_six_canonical_fields() -> None:
    """ShadowDecisionLog exposes the exact 6 fields named in the M2 spec."""
    log = ShadowDecisionLog(
        query_type="recent_context",
        selected_port="QueryPort",
        crosswalk_status="ok",
        arbitration_outcome="match",
        latency_ms=1.0,
        correlation_id="abc",
    )
    payload = json.loads(log.to_jsonl())
    assert set(payload.keys()) == {
        "query_type",
        "selected_port",
        "crosswalk_status",
        "arbitration_outcome",
        "latency_ms",
        "correlation_id",
    }


def test_decision_log_jsonl_has_sorted_keys() -> None:
    """to_jsonl() uses sort_keys so checksum-chain hashing is deterministic."""
    log = ShadowDecisionLog(
        query_type="recent_context",
        selected_port="QueryPort",
        crosswalk_status="ok",
        arbitration_outcome="match",
        latency_ms=1.0,
        correlation_id="abc",
    )
    line = log.to_jsonl()
    # Round-trip parse + re-dump with sort_keys must equal the original line.
    assert json.dumps(json.loads(line), sort_keys=True) == line


# ---------------------------------------------------------------------------
# Bypass paths
# ---------------------------------------------------------------------------


def test_bypass_when_flag_off(monkeypatch, shadow_log_dir) -> None:
    """SHADOW_MODE unset → interceptor is transparent; shadow factory not called."""
    monkeypatch.delenv("SHADOW_MODE", raising=False)
    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow_factory = MagicMock(side_effect=AssertionError("shadow must not be built"))

    interceptor = ShadowInterceptor(canonical, shadow_factory)
    result = interceptor.query(_make_request())

    assert result is canonical.evidence
    assert len(canonical.calls) == 1
    shadow_factory.assert_not_called()
    assert _read_log_lines(shadow_log_dir) == []


def test_allowlist_gates_request(monkeypatch, shadow_log_dir) -> None:
    """SHADOW_MODE=true but user not in SHADOW_USER_ALLOWLIST → bypass."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user_a,user_b")
    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow_factory = MagicMock(side_effect=AssertionError("shadow must not be built"))

    interceptor = ShadowInterceptor(canonical, shadow_factory)
    result = interceptor.query(_make_request(user_id="user1"))

    assert result is canonical.evidence
    shadow_factory.assert_not_called()
    assert _read_log_lines(shadow_log_dir) == []


def test_empty_allowlist_means_nobody(monkeypatch, shadow_log_dir) -> None:
    """SHADOW_MODE=true with empty allowlist → bypass for everyone."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "")
    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow_factory = MagicMock(side_effect=AssertionError("shadow must not be built"))

    interceptor = ShadowInterceptor(canonical, shadow_factory)
    interceptor.query(_make_request())

    shadow_factory.assert_not_called()
    assert _read_log_lines(shadow_log_dir) == []


# ---------------------------------------------------------------------------
# Active-shadow paths
# ---------------------------------------------------------------------------


def test_happy_path_records_match(monkeypatch, shadow_log_dir) -> None:
    """Canonical and shadow agree → log line records arbitration_outcome=match."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")
    evidence = _make_evidence("hit-1", "hit-2")
    canonical = _FakeRouter(evidence, [])
    shadow_router = _FakeRouter(evidence, [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)

    result = interceptor.query(_make_request())

    assert result is canonical.evidence
    assert len(canonical.calls) == 1
    assert len(shadow_router.calls) == 1

    lines = _read_log_lines(shadow_log_dir)
    assert len(lines) == 1
    assert lines[0]["arbitration_outcome"] == "match"
    assert lines[0]["crosswalk_status"] == "ok"
    assert lines[0]["query_type"] == "recent_context"
    assert lines[0]["selected_port"] == "QueryPort"


def test_divergence_captured_no_propagation(monkeypatch, shadow_log_dir) -> None:
    """Canonical and shadow disagree → caller still gets canonical; log records diverge."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")
    canonical_evidence = _make_evidence("canonical-hit")
    shadow_evidence = _make_evidence("different-hit")
    canonical = _FakeRouter(canonical_evidence, [])
    shadow_router = _FakeRouter(shadow_evidence, [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)

    result = interceptor.query(_make_request())

    # Caller MUST receive canonical, not shadow
    assert result is canonical_evidence
    assert result.hits[0]["id"] == "canonical-hit"

    lines = _read_log_lines(shadow_log_dir)
    assert len(lines) == 1
    assert lines[0]["arbitration_outcome"] == "diverge"


def test_shadow_exception_does_not_break_canonical(monkeypatch, shadow_log_dir) -> None:
    """Shadow path raising must not affect canonical result; log records shadow_only/skipped."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")
    canonical = _FakeRouter(_make_evidence("hit-1"), [])

    def _broken_shadow_factory():
        raise RuntimeError("boom")

    interceptor = ShadowInterceptor(canonical, _broken_shadow_factory)
    result = interceptor.query(_make_request())  # must not raise

    assert result is canonical.evidence
    lines = _read_log_lines(shadow_log_dir)
    assert len(lines) == 1
    assert lines[0]["arbitration_outcome"] == "shadow_only"
    assert lines[0]["crosswalk_status"] == "skipped"


def test_latency_recorded(monkeypatch, shadow_log_dir) -> None:
    """Latency is non-negative and bounded for in-memory test."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")
    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow_router = _FakeRouter(_make_evidence("hit-1"), [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)

    interceptor.query(_make_request())

    lines = _read_log_lines(shadow_log_dir)
    assert lines[0]["latency_ms"] >= 0.0
    assert lines[0]["latency_ms"] < 1000.0  # in-memory test should be sub-second


def test_correlation_id_propagation(monkeypatch, shadow_log_dir) -> None:
    """Caller-supplied correlation_id flows verbatim into the log."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")
    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow_router = _FakeRouter(_make_evidence("hit-1"), [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)

    interceptor.query(_make_request(), correlation_id="trace-abc-123")

    lines = _read_log_lines(shadow_log_dir)
    assert lines[0]["correlation_id"] == "trace-abc-123"


def test_correlation_id_auto_generated_when_absent(monkeypatch, shadow_log_dir) -> None:
    """No caller-supplied correlation_id → interceptor generates a non-empty UUID."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")
    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow_router = _FakeRouter(_make_evidence("hit-1"), [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)

    interceptor.query(_make_request())

    lines = _read_log_lines(shadow_log_dir)
    assert isinstance(lines[0]["correlation_id"], str)
    assert len(lines[0]["correlation_id"]) >= 8  # uuid4 hex is 32 chars; format may include dashes
