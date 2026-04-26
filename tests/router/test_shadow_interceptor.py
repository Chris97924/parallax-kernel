"""Lane C US-006 — Shadow Mode interceptor TDD coverage.

Covers: schema contract (9 fields), bypass logic, divergence capture,
latency / correlation_id propagation, FP-drift score tolerance, UTC daily
rotation, and one-time mkdir at construction (perf invariant).
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest
from parallax.router.shadow import SCHEMA_VERSION, ShadowDecisionLog, ShadowInterceptor
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeRouter:
    """Stub router that returns whatever evidence the test sets up."""

    evidence: RetrievalEvidence
    calls: list[QueryRequest]

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        self.calls.append(request)
        return self.evidence


def _make_evidence(*hit_ids: str, score: float = 0.9) -> RetrievalEvidence:
    return RetrievalEvidence(
        hits=tuple(
            {"id": hid, "kind": "memory", "score": score, "body": "", "text": hid}
            for hid in hit_ids
        ),
        stages=("test_stage",),
    )


def _make_evidence_with_scores(*pairs: tuple[str, float]) -> RetrievalEvidence:
    """pairs: each (hit_id, score)."""
    return RetrievalEvidence(
        hits=tuple(
            {"id": hid, "kind": "memory", "score": score, "body": "", "text": hid}
            for hid, score in pairs
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


_NINE_FIELDS = {
    "query_type",
    "selected_port",
    "crosswalk_status",
    "arbitration_outcome",
    "latency_ms",
    "correlation_id",
    "timestamp",
    "user_id",
    "schema_version",
}


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


def test_decision_log_has_nine_canonical_fields() -> None:
    """ShadowDecisionLog exposes exactly 9 fields after WS-3 schema extension."""
    log = ShadowDecisionLog(
        query_type="recent_context",
        selected_port="QueryPort",
        crosswalk_status="ok",
        arbitration_outcome="match",
        latency_ms=1.0,
        correlation_id="abc",
        timestamp="2026-04-26T10:30:45.123456+00:00",
        user_id="user1",
    )
    payload = json.loads(log.to_jsonl())
    assert set(payload.keys()) == _NINE_FIELDS


def test_decision_log_jsonl_has_sorted_keys() -> None:
    """to_jsonl() uses sort_keys so checksum-chain hashing is deterministic."""
    log = ShadowDecisionLog(
        query_type="recent_context",
        selected_port="QueryPort",
        crosswalk_status="ok",
        arbitration_outcome="match",
        latency_ms=1.0,
        correlation_id="abc",
        timestamp="2026-04-26T10:30:45.123456+00:00",
        user_id="user1",
    )
    line = log.to_jsonl()
    assert json.dumps(json.loads(line), sort_keys=True) == line


def test_decision_log_default_schema_version_is_one_zero() -> None:
    """schema_version defaults to '1.0' so callers do not have to set it."""
    log = ShadowDecisionLog(
        query_type="recent_context",
        selected_port="QueryPort",
        crosswalk_status="ok",
        arbitration_outcome="match",
        latency_ms=1.0,
        correlation_id="abc",
        timestamp="2026-04-26T10:30:45.123456+00:00",
        user_id="user1",
    )
    assert log.schema_version == "1.0"
    assert SCHEMA_VERSION == "1.0"


def test_interceptor_populates_timestamp_user_id_schema_version(
    monkeypatch, shadow_log_dir
) -> None:
    """All 3 new schema fields land in the JSONL via the interceptor path."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "alice")
    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow = _FakeRouter(_make_evidence("hit-1"), [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow)
    interceptor.query(_make_request(user_id="alice"))

    lines = _read_log_lines(shadow_log_dir)
    assert len(lines) == 1
    payload = lines[0]

    assert set(payload.keys()) == _NINE_FIELDS
    assert payload["user_id"] == "alice"
    assert payload["schema_version"] == "1.0"
    iso_pat = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$"
    assert re.match(iso_pat, payload["timestamp"]), payload["timestamp"]


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
    result = interceptor.query(_make_request())

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
    assert lines[0]["latency_ms"] < 1000.0


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
    assert len(lines[0]["correlation_id"]) >= 8


# ---------------------------------------------------------------------------
# FP-drift score tolerance (architect finding)
# ---------------------------------------------------------------------------


def test_hits_equal_treats_isclose_scores_as_match(monkeypatch, shadow_log_dir) -> None:
    """Score FP-drift within rel_tol=1e-6 must be marked match, not diverge."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")
    # 0.9 and 0.9 + 5e-7 differ by ~5.5e-7 relative — well inside 1e-6 tolerance.
    canonical = _FakeRouter(_make_evidence_with_scores(("hit-1", 0.9)), [])
    shadow_router = _FakeRouter(_make_evidence_with_scores(("hit-1", 0.9 + 5e-7)), [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)

    interceptor.query(_make_request())

    lines = _read_log_lines(shadow_log_dir)
    assert lines[0]["arbitration_outcome"] == "match"


def test_hits_equal_meaningful_score_drift_still_diverges(monkeypatch, shadow_log_dir) -> None:
    """Score divergence above rel_tol must still surface as diverge."""
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")
    canonical = _FakeRouter(_make_evidence_with_scores(("hit-1", 0.9)), [])
    shadow_router = _FakeRouter(_make_evidence_with_scores(("hit-1", 0.8)), [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)

    interceptor.query(_make_request())

    lines = _read_log_lines(shadow_log_dir)
    assert lines[0]["arbitration_outcome"] == "diverge"


# ---------------------------------------------------------------------------
# UTC daily rotation + mkdir cached at __init__
# ---------------------------------------------------------------------------


def test_log_filename_uses_utc_date(monkeypatch, tmp_path) -> None:
    """Daily file rotation uses UTC date, not local time."""
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")

    fake_struct = time.struct_time((2026, 4, 26, 12, 0, 0, 5, 116, 0))
    monkeypatch.setattr(time, "gmtime", lambda *a, **k: fake_struct)

    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow_router = _FakeRouter(_make_evidence("hit-1"), [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)
    interceptor.query(_make_request())

    expected = tmp_path / "shadow-decisions-2026-04-26.jsonl"
    assert expected.exists(), f"expected {expected}, got {list(tmp_path.iterdir())}"


def test_log_dir_mkdir_called_only_at_init(monkeypatch, tmp_path) -> None:
    """Path.mkdir is called once at __init__, not on every query() call."""
    log_dir = tmp_path / "shadow"
    monkeypatch.setenv("SHADOW_LOG_DIR", str(log_dir))
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("SHADOW_USER_ALLOWLIST", "user1")

    original_mkdir = pathlib.Path.mkdir
    call_count = {"n": 0}

    def counting_mkdir(self, *args, **kwargs):
        call_count["n"] += 1
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "mkdir", counting_mkdir)

    canonical = _FakeRouter(_make_evidence("hit-1"), [])
    shadow_router = _FakeRouter(_make_evidence("hit-1"), [])
    interceptor = ShadowInterceptor(canonical, lambda: shadow_router)
    init_count = call_count["n"]
    assert init_count >= 1, "expected mkdir during __init__"

    for _ in range(3):
        interceptor.query(_make_request())

    assert (
        call_count["n"] == init_count
    ), f"mkdir called {call_count['n'] - init_count} extra times across 3 queries"
