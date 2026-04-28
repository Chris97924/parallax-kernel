"""Tests for DualReadRouter (M3-T1.2, US-011).

Covers:
- 5×5 QueryType × outcome matrix
- Flag off / override behaviour
- Q5 CHANGE_TRACE.legacy_kind=bug short-circuit
- Timeout → aphelion_unreachable
- Unexpected secondary exception → primary_only
- Primary exception propagates
- Correlation ID generation / propagation
- Live counter integration
- SQLite cross-thread safety
- shadow.py module unmodified after a dual-read
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.aphelion_stub import AphelionReadAdapter, AphelionUnreachableError
from parallax.router.contracts import DualReadResult, QueryRequest
from parallax.router.discrepancy_live import LiveDiscrepancyCounter
from parallax.router.dual_read import DualReadRouter
from parallax.router.ports import QueryPort
from parallax.router.sqlite_gate import SQLiteGate
from parallax.router.types import QueryType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(*ids: str) -> RetrievalEvidence:
    hits = tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids)
    return RetrievalEvidence(hits=hits, stages=("test",))


def _evidence_scored(*pairs: tuple[str, float]) -> RetrievalEvidence:
    hits = tuple({"id": i, "kind": "memory", "score": s} for i, s in pairs)
    return RetrievalEvidence(hits=hits, stages=("test",))


class _StubPort:
    """Synchronous stub QueryPort — returns a fixed RetrievalEvidence."""

    def __init__(self, result: RetrievalEvidence) -> None:
        self._result = result
        self.call_count = 0

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        self.call_count += 1
        return self._result


class _RaisingPort:
    """QueryPort that raises a given exception on query()."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.call_count = 0

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        self.call_count += 1
        raise self._exc


class _SleepingPort:
    """QueryPort that sleeps for ms milliseconds before returning."""

    def __init__(self, result: RetrievalEvidence, delay_ms: float) -> None:
        self._result = result
        self._delay_ms = delay_ms
        self.call_count = 0

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        self.call_count += 1
        time.sleep(self._delay_ms / 1000.0)
        return self._result


def _router(
    primary: QueryPort,
    secondary: QueryPort,
    *,
    live_counter: LiveDiscrepancyCounter | None = None,
    timeout_ms: float = 500.0,
) -> DualReadRouter:
    return DualReadRouter(
        primary=primary,
        secondary=secondary,
        live_counter=live_counter,
        secondary_timeout_ms=timeout_ms,
    )


def _request(
    qt: QueryType = QueryType.RECENT_CONTEXT,
    *,
    params: dict | None = None,
) -> QueryRequest:
    return QueryRequest(query_type=qt, user_id="u1", params=params)


# ---------------------------------------------------------------------------
# 5×5 QueryType × outcome matrix
# ---------------------------------------------------------------------------

_ALL_QT = list(QueryType)
_N_HITS = 2  # number of hits in the "N hits" scenarios


def _n_hits() -> RetrievalEvidence:
    return _evidence(*[f"id{i}" for i in range(_N_HITS)])


def _n_plus_one_hits() -> RetrievalEvidence:
    return _evidence(*[f"id{i}" for i in range(_N_HITS + 1)])


def _n_hits_different_score() -> RetrievalEvidence:
    """Same IDs, score differs by > 1e-5."""
    return _evidence_scored(*[(f"id{i}", 2.0) for i in range(_N_HITS)])


def _n_hits_close_score() -> RetrievalEvidence:
    """Same IDs, score differs by < 1e-7 (within rel_tol 1e-6 → match)."""
    base = 1.0
    drift = base * 5e-7  # relative drift < 1e-6
    return _evidence_scored(*[(f"id{i}", base + drift) for i in range(_N_HITS)])


@pytest.mark.parametrize("qt", _ALL_QT)
def test_matrix_match(qt: QueryType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request(qt))
    assert r.outcome == "match"
    assert r.primary is not None
    assert r.secondary is not None


@pytest.mark.parametrize("qt", _ALL_QT)
def test_matrix_diverge_extra_hit(qt: QueryType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_plus_one_hits())
    r = _router(primary, secondary).query(_request(qt))
    assert r.outcome == "diverge"


@pytest.mark.parametrize("qt", _ALL_QT)
def test_matrix_diverge_different_score(qt: QueryType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits_different_score())
    r = _router(primary, secondary).query(_request(qt))
    assert r.outcome == "diverge"


@pytest.mark.parametrize("qt", _ALL_QT)
def test_matrix_match_close_score(qt: QueryType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits_close_score())
    r = _router(primary, secondary).query(_request(qt))
    assert r.outcome == "match", f"Hits within rel_tol should be 'match', got {r.outcome!r}"


@pytest.mark.parametrize("qt", _ALL_QT)
def test_matrix_aphelion_unreachable(qt: QueryType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _RaisingPort(AphelionUnreachableError("not_implemented"))
    r = _router(primary, secondary).query(_request(qt))
    assert r.outcome == "aphelion_unreachable"
    assert r.primary is not None
    assert r.secondary is None


# ---------------------------------------------------------------------------
# Behaviour tests
# ---------------------------------------------------------------------------


def test_flag_off_skips_secondary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request())
    assert r.outcome == "skipped"
    assert secondary.call_count == 0


def test_flag_off_zero_overhead(monkeypatch: pytest.MonkeyPatch) -> None:
    """When flag is off, secondary must NEVER be called."""
    monkeypatch.delenv("DUAL_READ", raising=False)
    primary = _StubPort(_n_hits())
    secondary_mock = MagicMock(spec=QueryPort)
    _router(primary, secondary_mock).query(_request())
    secondary_mock.query.assert_not_called()


def test_change_trace_legacy_kind_bug_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q5: CHANGE_TRACE + legacy_kind=bug → skipped, secondary not called."""
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    req = _request(QueryType.CHANGE_TRACE, params={"legacy_kind": "bug"})
    r = _router(primary, secondary).query(req)
    assert r.outcome == "skipped"
    assert secondary.call_count == 0


def test_change_trace_legacy_kind_decision_does_not_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    req = _request(QueryType.CHANGE_TRACE, params={"legacy_kind": "decision"})
    r = _router(primary, secondary).query(req)
    assert r.outcome != "skipped"
    assert secondary.call_count >= 1


def test_change_trace_no_params_does_not_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    req = _request(QueryType.CHANGE_TRACE, params=None)
    r = _router(primary, secondary).query(req)
    assert r.outcome != "skipped"
    assert secondary.call_count >= 1


def test_secondary_timeout_classified_as_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _SleepingPort(_n_hits(), delay_ms=300)
    r = _router(primary, secondary, timeout_ms=50).query(_request())
    assert r.outcome == "aphelion_unreachable"
    assert r.aphelion_unreachable_reason == "timeout"
    assert r.primary is not None


def test_secondary_unexpected_exception_classified_as_primary_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _RaisingPort(RuntimeError("oops"))
    r = _router(primary, secondary).query(_request())
    assert r.outcome == "primary_only"
    assert r.primary is not None
    assert r.secondary is None


def test_primary_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Primary failure must propagate — don't swallow canonical errors."""
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _RaisingPort(ValueError("primary broke"))
    secondary = _StubPort(_n_hits())
    with pytest.raises(ValueError, match="primary broke"):
        _router(primary, secondary).query(_request())


def test_dual_read_override_true_overrides_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request(), dual_read_override=True)
    assert r.outcome != "skipped"
    assert secondary.call_count >= 1


def test_dual_read_override_false_overrides_env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request(), dual_read_override=False)
    assert r.outcome == "skipped"
    assert secondary.call_count == 0


def test_correlation_id_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request(), correlation_id="abc")
    assert r.correlation_id == "abc"


def test_correlation_id_generated_if_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request(), correlation_id=None)
    assert r.correlation_id != ""
    # Must be a valid UUID4
    parsed = uuid.UUID(r.correlation_id, version=4)
    assert str(parsed) == r.correlation_id


def test_live_counter_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    counter = MagicMock(spec=LiveDiscrepancyCounter)
    _router(primary, secondary, live_counter=counter).query(_request())
    counter.record.assert_called_once()
    call_kwargs = counter.record.call_args[1]
    assert call_kwargs["outcome"] == "match"


def test_live_counter_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    # No exception when live_counter=None
    r = _router(primary, secondary, live_counter=None).query(_request())
    assert r.outcome == "match"


def test_sqlite_cross_thread_safety(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Primary uses a SQLiteGate-wrapped connection; 20 concurrent dual-reads
    must not raise sqlite3.ProgrammingError (the cross-thread assertion)."""
    monkeypatch.setenv("DUAL_READ", "true")

    db_path = tmp_path / "dual_read_test.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    SQLiteGate._pragma_applied.discard(id(conn))
    gate = SQLiteGate(conn, component="m3_dual_read")

    # Create a minimal schema; primary is a stub that uses gate.fetch_all.
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()

    class GatePort:
        def query(self, request: QueryRequest) -> RetrievalEvidence:
            rows = gate.fetch_all("SELECT n FROM t")
            hits = tuple({"id": str(r[0]), "kind": "memory", "score": 1.0} for r in rows)
            return RetrievalEvidence(hits=hits, stages=("gate",))

    primary = GatePort()
    secondary = _RaisingPort(AphelionUnreachableError("not_implemented"))
    router = _router(primary, secondary, timeout_ms=200)

    errors: list[Exception] = []

    def worker():
        try:
            r = router.query(_request(), dual_read_override=True)
            assert r.primary is not None
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    SQLiteGate._pragma_applied.discard(id(conn))
    conn.close()

    programming_errors = [e for e in errors if isinstance(e, sqlite3.ProgrammingError)]
    assert (
        programming_errors == []
    ), f"sqlite3.ProgrammingError raised in cross-thread test: {programming_errors}"
    assert errors == [], f"Errors in cross-thread test: {errors[:3]}"


def test_does_not_modify_shadow_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running a dual-read must not monkey-patch parallax.router.shadow."""
    import parallax.router.shadow as shadow_mod

    before_dir = frozenset(dir(shadow_mod))

    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    _router(primary, secondary).query(_request())

    after_dir = frozenset(dir(shadow_mod))
    assert (
        before_dir == after_dir
    ), f"shadow module dir() changed after dual-read: added={after_dir - before_dir}"


# ---------------------------------------------------------------------------
# DualReadResult fields populated correctly
# ---------------------------------------------------------------------------


def test_result_is_dual_read_result_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request())
    assert isinstance(r, DualReadResult)


def test_latency_primary_ms_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request())
    assert r.latency_primary_ms >= 0.0


def test_latency_secondary_ms_set_on_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = _StubPort(_n_hits())
    r = _router(primary, secondary).query(_request())
    assert r.latency_secondary_ms is not None
    assert r.latency_secondary_ms >= 0.0


def test_aphelion_unreachable_reason_on_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ", "true")
    primary = _StubPort(_n_hits())
    secondary = AphelionReadAdapter()
    r = _router(primary, secondary).query(_request())
    assert r.outcome == "aphelion_unreachable"
    assert r.aphelion_unreachable_reason == "not_implemented"
