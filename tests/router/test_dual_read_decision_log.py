"""TDD coverage for ``parallax.router.dual_read_decision_log`` (M3b
post-review JSONL-PRODUCER).

Verifies:
- append creates the file when not yet present + idempotent line append
- daily file rollover on UTC midnight
- all required fields (per module docstring) present
- skipped outcome → winning_source=null
- dual_attempted with fallback → winning_source='fallback' + optional cid
- schema_version locked at "1.0"
- deterministic JSON: same inputs + same anchor → byte-equal lines
- ``DUAL_READ_LOG_ENABLED=false`` short-circuits with no file written
- ``log_dir`` override honored over env var
- corrupted prior line in the file does not break the appender
- ``DUAL_READ_LOG_DIR`` env honored when no explicit log_dir
- best-effort: I/O exception swallowed (no raise out of the function)
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from parallax.router import dual_read_decision_log as ddlog


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a known-on log + no env-driven log_dir."""
    monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "true")
    monkeypatch.delenv("DUAL_READ_LOG_DIR", raising=False)
    monkeypatch.delenv("DUAL_READ", raising=False)


def _decision(**overrides: object) -> dict[str, object]:
    """Build a default decision record for the tests."""
    base: dict[str, object] = {
        "correlation_id": "cid-1",
        "query_type": "recent_context",
        "outcome": "dual_attempted",
        "winning_source": "parallax",
        "policy_version": "v0.3.0-rc",
        "write_error_observed": False,
        "conflict_event_id": None,
        "data_quality_flag": "normal",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Append + daily file
# ---------------------------------------------------------------------------


def test_append_creates_file_when_not_exist(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    path = ddlog.append_decision(_decision(), log_dir=tmp_path, now=anchor)
    assert path is not None
    assert path.is_file()
    assert path.name == "dual-read-decisions-2026-04-30.jsonl"


def test_append_is_idempotent_line_by_line(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    ddlog.append_decision(_decision(correlation_id="a"), log_dir=tmp_path, now=anchor)
    ddlog.append_decision(_decision(correlation_id="b"), log_dir=tmp_path, now=anchor)
    ddlog.append_decision(_decision(correlation_id="c"), log_dir=tmp_path, now=anchor)
    path = tmp_path / "dual-read-decisions-2026-04-30.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 3
    cids = [json.loads(line)["correlation_id"] for line in lines]
    assert cids == ["a", "b", "c"]


def test_daily_rollover_on_utc_midnight(tmp_path: Path) -> None:
    a = _dt.datetime(2026, 4, 30, 23, 59, 59, 500_000, tzinfo=_dt.UTC)
    b = _dt.datetime(2026, 5, 1, 0, 0, 0, tzinfo=_dt.UTC)
    p1 = ddlog.append_decision(_decision(correlation_id="A"), log_dir=tmp_path, now=a)
    p2 = ddlog.append_decision(_decision(correlation_id="B"), log_dir=tmp_path, now=b)
    assert p1 is not None and p1.name == "dual-read-decisions-2026-04-30.jsonl"
    assert p2 is not None and p2.name == "dual-read-decisions-2026-05-01.jsonl"
    assert p1 != p2


# ---------------------------------------------------------------------------
# Required fields + schema_version
# ---------------------------------------------------------------------------


def test_all_required_fields_present(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    path = ddlog.append_decision(_decision(), log_dir=tmp_path, now=anchor)
    assert path is not None
    line = path.read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    for field in (
        "schema_version",
        "timestamp_us_utc",
        "timestamp",
        "correlation_id",
        "query_type",
        "outcome",
        "winning_source",
        "policy_version",
        "write_error_observed",
        "conflict_event_id",
        "data_quality_flag",
    ):
        assert field in record, f"missing {field}"
    assert record["schema_version"] == "1.0"


def test_schema_version_always_locked_at_1_0(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    # Caller-supplied schema_version is ignored — writer owns this field.
    custom = _decision()
    custom["schema_version"] = "999.0"  # type: ignore[assignment]
    path = ddlog.append_decision(custom, log_dir=tmp_path, now=anchor)
    assert path is not None
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Outcome / winning_source / fallback semantics
# ---------------------------------------------------------------------------


def test_skipped_outcome_winning_source_null(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    rec = _decision(outcome="skipped", winning_source=None)
    path = ddlog.append_decision(rec, log_dir=tmp_path, now=anchor)
    assert path is not None
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["outcome"] == "skipped"
    assert record["winning_source"] is None


def test_dual_attempted_fallback_with_event_id(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    rec = _decision(
        outcome="dual_attempted",
        winning_source="fallback",
        conflict_event_id="ev-abc",
    )
    path = ddlog.append_decision(rec, log_dir=tmp_path, now=anchor)
    assert path is not None
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["winning_source"] == "fallback"
    assert record["conflict_event_id"] == "ev-abc"


def test_dual_attempted_fallback_without_event_id(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    rec = _decision(
        outcome="dual_attempted",
        winning_source="fallback",
        conflict_event_id=None,
    )
    path = ddlog.append_decision(rec, log_dir=tmp_path, now=anchor)
    assert path is not None
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["winning_source"] == "fallback"
    assert record["conflict_event_id"] is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_two_runs_byte_equal(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    rec = _decision()
    p1 = ddlog.append_decision(rec, log_dir=tmp_path, now=anchor)
    line1 = p1.read_text(encoding="utf-8") if p1 else ""
    # Wipe and re-run with same anchor + same record.
    p1.unlink()  # type: ignore[union-attr]
    p2 = ddlog.append_decision(rec, log_dir=tmp_path, now=anchor)
    line2 = p2.read_text(encoding="utf-8") if p2 else ""
    assert line1 == line2


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def test_feature_flag_off_no_file_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DUAL_READ_LOG_ENABLED", "false")
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    result = ddlog.append_decision(_decision(), log_dir=tmp_path, now=anchor)
    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_feature_flag_mirror_dual_read_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without explicit DUAL_READ_LOG_ENABLED, mirror DUAL_READ.

    DUAL_READ=false → off; DUAL_READ=true → on.
    """
    monkeypatch.delenv("DUAL_READ_LOG_ENABLED", raising=False)
    monkeypatch.setenv("DUAL_READ", "false")
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    assert ddlog.append_decision(_decision(), log_dir=tmp_path, now=anchor) is None

    monkeypatch.setenv("DUAL_READ", "true")
    assert ddlog.append_decision(_decision(), log_dir=tmp_path, now=anchor) is not None


# ---------------------------------------------------------------------------
# log_dir override / env var
# ---------------------------------------------------------------------------


def test_log_dir_override_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit log_dir takes precedence over DUAL_READ_LOG_DIR env."""
    env_dir = tmp_path / "from_env"
    arg_dir = tmp_path / "from_arg"
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(env_dir))
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    path = ddlog.append_decision(_decision(), log_dir=arg_dir, now=anchor)
    assert path is not None
    assert arg_dir.resolve() in path.resolve().parents
    assert not env_dir.exists()


def test_dual_read_log_dir_env_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without log_dir argument, DUAL_READ_LOG_DIR env is used."""
    env_dir = tmp_path / "from_env"
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(env_dir))
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    path = ddlog.append_decision(_decision(), now=anchor)
    assert path is not None
    assert env_dir.resolve() in path.resolve().parents


# ---------------------------------------------------------------------------
# Robustness: corrupted prior line + I/O failure
# ---------------------------------------------------------------------------


def test_corrupted_prior_line_does_not_break_append(tmp_path: Path) -> None:
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    # Pre-seed the daily file with a corrupt prior line.
    daily = tmp_path / "dual-read-decisions-2026-04-30.jsonl"
    daily.write_text("not-json-at-all\n", encoding="utf-8")
    path = ddlog.append_decision(_decision(), log_dir=tmp_path, now=anchor)
    assert path is not None
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "not-json-at-all"
    # New line was appended.
    assert json.loads(lines[1])["correlation_id"] == "cid-1"


def test_io_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the directory cannot be created, the writer returns None (no raise)."""

    def _bad_resolve(_log_dir: Path | str | None = None) -> Path:
        raise PermissionError("simulated denied")

    monkeypatch.setattr(ddlog, "resolve_log_dir", _bad_resolve)
    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    result = ddlog.append_decision(_decision(), now=anchor)
    assert result is None


# ---------------------------------------------------------------------------
# Records readable by parallax.router.dual_read_metrics
# ---------------------------------------------------------------------------


def test_records_round_trip_through_metrics_load_records(tmp_path: Path) -> None:
    """A line written here must parse cleanly via dual_read_metrics.load_records."""
    from parallax.router import dual_read_metrics as m

    anchor = _dt.datetime(2026, 4, 30, 12, 0, tzinfo=_dt.UTC)
    ddlog.append_decision(
        _decision(outcome="diverge", winning_source="parallax"),
        log_dir=tmp_path,
        now=anchor,
    )
    result = m.load_records(log_dir=tmp_path)
    assert result.dir_missing is False
    assert result.malformed == 0
    assert len(result.records) == 1
    rec = result.records[0]
    assert rec["correlation_id"] == "cid-1"
    assert rec["winning_source"] == "parallax"
