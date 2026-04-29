"""TDD coverage for ``parallax.router.dual_read_metrics`` (M3b — US-006).

The dual-read metrics module exposes 6 file-based rate computations over the
dual-read decision JSONL stream so the DoD CLI + Prometheus endpoint share
one source of truth (DRY thresholds + parsing).

Contract:
- All denominators exclude ``aphelion_unreachable`` events (mirrors M2's
  shadow_only exclusion per ralplan §6 line 429).
- ``data_quality_filter`` defaults to ``["normal", "corpus_immature"]`` —
  ``cold_start`` records are excluded from production rate calculations.
- Empty window → 0.0 (rates) / 0 (counts).
- Each function accepts ``window: str`` plus optional ``log_dir``, ``now``,
  ``data_quality_filter``.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any


def _write(log_dir: Path, records: list[dict[str, Any]], date: str = "2026-04-26") -> Path:
    """Append dual-read decision records to a daily JSONL file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"dual-read-decisions-{date}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return path


def _record(
    *,
    outcome: str = "match",
    timestamp: str = "2026-04-26T11:00:00.000000+00:00",
    data_quality_flag: str = "normal",
    crosswalk_status: str = "ok",
    circuit_breaker_tripped: bool = False,
    write_error: bool = False,
    correlation_id: str = "cid-1",
    user_id: str = "u1",
    **extra: Any,
) -> dict[str, Any]:
    record = {
        "outcome": outcome,
        "timestamp": timestamp,
        "data_quality_flag": data_quality_flag,
        "crosswalk_status": crosswalk_status,
        "circuit_breaker_tripped": circuit_breaker_tripped,
        "write_error": write_error,
        "correlation_id": correlation_id,
        "user_id": user_id,
    }
    record.update(extra)
    return record


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_exports_thresholds_and_functions() -> None:
    from parallax.router import dual_read_metrics as m

    assert m.DISCREPANCY_RATE_THRESHOLD_M3 == 0.001
    assert m.ARBITRATION_CONFLICT_RATE_THRESHOLD == 0.01
    assert m.WRITE_ERROR_RATE_THRESHOLD == 0.0002
    assert m.APHELION_UNREACHABLE_THRESHOLD == 0.005
    assert m.CROSSWALK_MISS_THRESHOLD == 0.05
    assert m.CIRCUIT_OPEN_72H_MAX == 3

    # Functions exist
    for name in (
        "discrepancy_rate",
        "arbitration_conflict_rate",
        "write_error_rate",
        "aphelion_unreachable_rate",
        "crosswalk_miss_rate",
        "circuit_open_count",
    ):
        assert hasattr(m, name), f"missing {name}"


# ---------------------------------------------------------------------------
# Empty window → 0.0 / 0
# ---------------------------------------------------------------------------


def test_empty_log_dir_returns_zero(tmp_path: Path) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    assert m.discrepancy_rate("1h", log_dir=tmp_path, now=now) == 0.0
    assert m.arbitration_conflict_rate("1h", log_dir=tmp_path, now=now) == 0.0
    assert m.write_error_rate("1h", log_dir=tmp_path, now=now) == 0.0
    assert m.aphelion_unreachable_rate("1h", log_dir=tmp_path, now=now) == 0.0
    assert m.crosswalk_miss_rate("1h", log_dir=tmp_path, now=now) == 0.0
    assert m.circuit_open_count("1h", log_dir=tmp_path, now=now) == 0


# ---------------------------------------------------------------------------
# discrepancy_rate
# ---------------------------------------------------------------------------


def test_discrepancy_rate_counts_diverge_over_total_excluding_unreachable(
    tmp_path: Path,
) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    records = (
        [_record(outcome="match", timestamp=ts) for _ in range(95)]
        + [_record(outcome="diverge", timestamp=ts) for _ in range(5)]
        + [
            # Excluded from denominator
            _record(outcome="aphelion_unreachable", timestamp=ts)
            for _ in range(20)
        ]
    )
    _write(tmp_path, records)
    rate = m.discrepancy_rate("1h", log_dir=tmp_path, now=now)
    assert rate == 0.05  # 5 / (95 + 5) = 0.05


def test_discrepancy_rate_excludes_cold_start_by_default(tmp_path: Path) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    records = [
        _record(outcome="match", timestamp=ts, data_quality_flag="normal"),
        _record(outcome="diverge", timestamp=ts, data_quality_flag="normal"),
        # cold_start records: should be excluded from prod rate calc
        _record(outcome="diverge", timestamp=ts, data_quality_flag="cold_start"),
        _record(outcome="diverge", timestamp=ts, data_quality_flag="cold_start"),
        _record(outcome="diverge", timestamp=ts, data_quality_flag="cold_start"),
    ]
    _write(tmp_path, records)
    rate = m.discrepancy_rate("1h", log_dir=tmp_path, now=now)
    # Default filter: ["normal", "corpus_immature"]; cold_start excluded.
    # 1 diverge / (1 match + 1 diverge) = 0.5
    assert rate == 0.5


def test_discrepancy_rate_respects_explicit_filter(tmp_path: Path) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    records = [
        _record(outcome="match", timestamp=ts, data_quality_flag="normal"),
        _record(outcome="diverge", timestamp=ts, data_quality_flag="cold_start"),
    ]
    _write(tmp_path, records)
    # With cold_start INCLUDED:
    rate = m.discrepancy_rate(
        "1h",
        log_dir=tmp_path,
        now=now,
        data_quality_filter=["normal", "corpus_immature", "cold_start"],
    )
    assert rate == 0.5


# ---------------------------------------------------------------------------
# arbitration_conflict_rate
# ---------------------------------------------------------------------------


def test_arbitration_conflict_rate_counts_tie_and_fallback(tmp_path: Path) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    records = [
        # 100 dual-read attempts: 2 produce conflict events
        _record(outcome="match", timestamp=ts, winning_source="parallax")
        for _ in range(98)
    ]
    records.extend(
        _record(outcome="diverge", timestamp=ts, winning_source="fallback") for _ in range(2)
    )
    # `aphelion_unreachable` excluded from denominator
    records.extend(_record(outcome="aphelion_unreachable", timestamp=ts) for _ in range(50))
    _write(tmp_path, records)
    rate = m.arbitration_conflict_rate("1h", log_dir=tmp_path, now=now)
    assert rate == 0.02  # 2 / 100


# ---------------------------------------------------------------------------
# write_error_rate
# ---------------------------------------------------------------------------


def test_write_error_rate_counts_write_error_records(tmp_path: Path) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    records = [_record(outcome="match", timestamp=ts) for _ in range(99)]
    records.append(_record(outcome="match", timestamp=ts, write_error=True))
    records.extend(_record(outcome="aphelion_unreachable", timestamp=ts) for _ in range(40))
    _write(tmp_path, records)
    rate = m.write_error_rate("1h", log_dir=tmp_path, now=now)
    assert rate == 0.01  # 1 / 100


# ---------------------------------------------------------------------------
# aphelion_unreachable_rate
# ---------------------------------------------------------------------------


def test_aphelion_unreachable_rate_uses_total_denominator(tmp_path: Path) -> None:
    """``aphelion_unreachable_rate`` denominator = ALL outcomes (NOT excluded)."""
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    records = [_record(outcome="match", timestamp=ts) for _ in range(95)]
    records.extend(_record(outcome="aphelion_unreachable", timestamp=ts) for _ in range(5))
    _write(tmp_path, records)
    rate = m.aphelion_unreachable_rate("1h", log_dir=tmp_path, now=now)
    assert rate == 0.05  # 5 / 100


# ---------------------------------------------------------------------------
# crosswalk_miss_rate
# ---------------------------------------------------------------------------


def test_crosswalk_miss_rate_counts_miss_status(tmp_path: Path) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    records = [_record(outcome="match", timestamp=ts, crosswalk_status="ok") for _ in range(90)]
    records.extend(
        _record(outcome="match", timestamp=ts, crosswalk_status="miss") for _ in range(10)
    )
    _write(tmp_path, records)
    rate = m.crosswalk_miss_rate("1h", log_dir=tmp_path, now=now)
    assert rate == 0.10


# ---------------------------------------------------------------------------
# circuit_open_count
# ---------------------------------------------------------------------------


def test_circuit_open_count_counts_tripped_records(tmp_path: Path) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    records = [_record(outcome="match", timestamp=ts) for _ in range(50)]
    records.extend(
        _record(outcome="match", timestamp=ts, circuit_breaker_tripped=True) for _ in range(2)
    )
    _write(tmp_path, records)
    count = m.circuit_open_count("72h", log_dir=tmp_path, now=now)
    assert count == 2


# ---------------------------------------------------------------------------
# Window filter: out-of-window records dropped
# ---------------------------------------------------------------------------


def test_window_filter_drops_old_records(tmp_path: Path) -> None:
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    # In-window:
    in_records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(2)
    ]
    # Out-of-window (2 days ago):
    out_records = [
        _record(outcome="diverge", timestamp="2026-04-24T10:00:00.000000+00:00") for _ in range(50)
    ]
    _write(tmp_path, in_records, date="2026-04-26")
    _write(tmp_path, out_records, date="2026-04-24")
    rate = m.discrepancy_rate("1h", log_dir=tmp_path, now=now)
    # Only in-window records considered: 0/2 = 0.0
    assert rate == 0.0


# ---------------------------------------------------------------------------
# Tolerates ``arbitration_outcome`` field name (shadow JSONL stream compat)
# ---------------------------------------------------------------------------


def test_alternate_outcome_field_arbitration_outcome(tmp_path: Path) -> None:
    """Records using shadow's ``arbitration_outcome`` field must parse too."""
    from parallax.router import dual_read_metrics as m

    now = _dt.datetime(2026, 4, 26, 12, 0, tzinfo=_dt.UTC)
    ts = "2026-04-26T11:30:00.000000+00:00"
    rec = {
        "arbitration_outcome": "diverge",
        "timestamp": ts,
        "data_quality_flag": "normal",
    }
    _write(tmp_path, [rec, {**rec, "arbitration_outcome": "match"}])
    rate = m.discrepancy_rate("1h", log_dir=tmp_path, now=now)
    assert rate == 0.5


# ---------------------------------------------------------------------------
# H5 — load_records returns _LoadResult with dir_missing + malformed counters
# ---------------------------------------------------------------------------


def test_load_records_returns_loadresult_with_dir_missing_when_path_missing(
    tmp_path: Path,
) -> None:
    """Story H5 — missing dir flagged via dir_missing=True."""
    from parallax.router import dual_read_metrics as m

    missing = tmp_path / "does_not_exist"
    result = m.load_records(log_dir=missing)
    assert result.records == []
    assert result.dir_missing is True
    assert result.malformed == 0


def test_load_records_returns_dir_missing_false_when_dir_exists(tmp_path: Path) -> None:
    """Story H5 — existing dir reports dir_missing=False even when empty."""
    from parallax.router import dual_read_metrics as m

    result = m.load_records(log_dir=tmp_path)
    assert result.records == []
    assert result.dir_missing is False
    assert result.malformed == 0


def test_compute_all_rates_returns_zeroes_on_empty_records() -> None:
    """MED-LOWS-BUNDLED — compute_all_rates handles empty record list."""
    from parallax.router import dual_read_metrics as m

    result = m.compute_all_rates([])
    assert result["discrepancy_rate"] == 0.0
    assert result["arbitration_conflict_rate"] == 0.0
    assert result["write_error_rate"] == 0.0
    assert result["aphelion_unreachable_rate"] == 0.0
    assert result["crosswalk_miss_rate"] == 0.0
    assert result["circuit_open_count"] == 0


def test_compute_all_rates_counts_winning_source_tie_and_fallback() -> None:
    """MED-LOWS-BUNDLED — winning_source ∈ {tie, fallback} → conflict.

    Also covers the conflict_event_id branch (non-tie/fallback record with
    a populated conflict_event_id is still a conflict).
    """
    import pytest as _pytest

    from parallax.router import dual_read_metrics as m

    records = [
        {
            "outcome": "dual_attempted",
            "winning_source": "tie",
            "data_quality_flag": "normal",
        },
        {
            "outcome": "dual_attempted",
            "winning_source": "parallax",
            "conflict_event_id": "ev-x",
            "data_quality_flag": "normal",
        },
        {
            "outcome": "match",
            "winning_source": "parallax",
            "data_quality_flag": "normal",
        },
    ]
    result = m.compute_all_rates(records)
    # 2 conflicts / 3 denom = 0.6666...
    assert result["arbitration_conflict_rate"] == _pytest.approx(2 / 3)


def test_load_records_counts_malformed(tmp_path: Path) -> None:
    """MED-MALFORMED-COUNTER — 3 valid + 2 corrupt → malformed=2, records=3."""
    from parallax.router import dual_read_metrics as m

    log_dir = tmp_path
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "dual-read-decisions-2026-04-26.jsonl"
    valid = _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00")
    with path.open("w", encoding="utf-8") as fh:
        for _ in range(3):
            fh.write(json.dumps(valid, sort_keys=True) + "\n")
        fh.write("not-json-at-all\n")
        fh.write("{still-broken}\n")
    result = m.load_records(log_dir=log_dir)
    assert len(result.records) == 3
    assert result.malformed == 2
    assert result.dir_missing is False
