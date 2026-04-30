"""TDD coverage for ``scripts/dual_read_continuity_check.py`` (M3b — US-006).

Mirrors ``tests/shadow/test_continuity_check.py`` style. Verifies the 6
DoD threshold checks: discrepancy_rate, arbitration_conflict_rate,
write_error_rate, aphelion_unreachable_rate, crosswalk_miss_rate,
circuit_open_count.

Exit code semantics:
- 0 iff every threshold met AND record count >= ``--min-records``
- 1 otherwise (with the breach name in ``failures``)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dual_read_continuity_check.py"


def _run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _write(log_dir: Path, records: list[dict[str, Any]], date: str = "2026-04-26") -> Path:
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
    write_error_observed: bool = False,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "timestamp": timestamp,
        "data_quality_flag": data_quality_flag,
        "crosswalk_status": crosswalk_status,
        "circuit_breaker_tripped": circuit_breaker_tripped,
        "write_error_observed": write_error_observed,
    }


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"missing CLI: {SCRIPT}"


def test_help_runs() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "dual_read_continuity_check" in result.stdout or "since" in result.stdout


# ---------------------------------------------------------------------------
# Empty log + min_records=0 → pass with skip flag
# ---------------------------------------------------------------------------


def test_empty_log_min_records_zero_passes(tmp_path: Path) -> None:
    """Empty log dir + min-records=0 → exit 0 (no breach)."""
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=0",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["total_records"] == 0
    assert payload["passed"] is True


def test_empty_log_min_records_positive_fails(tmp_path: Path) -> None:
    """Empty log dir + min-records=1 → exit 1 (no shadow activity = DoD fail)."""
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["passed"] is False
    assert "min_records" in payload["failures"]


# ---------------------------------------------------------------------------
# All-pass smoke
# ---------------------------------------------------------------------------


def test_all_match_passes_dod(tmp_path: Path) -> None:
    """1000 match records → DoD passes, exit 0."""
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(1000)
    ]
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["total_records"] == 1000
    assert payload["passed"] is True
    assert payload["failures"] == []


# ---------------------------------------------------------------------------
# Individual breach cases
# ---------------------------------------------------------------------------


def test_high_discrepancy_fails(tmp_path: Path) -> None:
    """5/100 diverge = 5% > 0.1% threshold → exit 1 with discrepancy_rate breach."""
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(95)
    ]
    records.extend(
        _record(outcome="diverge", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(5)
    )
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert "discrepancy_rate" in payload["failures"]


def test_high_aphelion_unreachable_fails(tmp_path: Path) -> None:
    """6/100 unreachable = 6% > 0.5% threshold."""
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(94)
    ]
    records.extend(
        _record(outcome="aphelion_unreachable", timestamp="2026-04-26T11:30:00.000000+00:00")
        for _ in range(6)
    )
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert "aphelion_unreachable_rate" in payload["failures"]


def test_high_crosswalk_miss_fails(tmp_path: Path) -> None:
    """10/100 miss = 10% > 5% threshold."""
    records = [
        _record(
            outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00", crosswalk_status="ok"
        )
        for _ in range(90)
    ]
    records.extend(
        _record(
            outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00", crosswalk_status="miss"
        )
        for _ in range(10)
    )
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert "crosswalk_miss_rate" in payload["failures"]


def test_high_circuit_open_count_fails(tmp_path: Path) -> None:
    """4 circuit-open records > 3 cap → exit 1."""
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(96)
    ]
    records.extend(
        _record(
            outcome="match",
            timestamp="2026-04-26T11:30:00.000000+00:00",
            circuit_breaker_tripped=True,
        )
        for _ in range(4)
    )
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert "circuit_open_count" in payload["failures"]


# ---------------------------------------------------------------------------
# JSON format round-trip + threshold override + --now determinism
# ---------------------------------------------------------------------------


def test_json_format_round_trip(tmp_path: Path) -> None:
    records = [_record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00")]
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    # Required keys present
    for key in (
        "since",
        "log_dir",
        "total_records",
        "discrepancy_rate",
        "arbitration_conflict_rate",
        "write_error_rate",
        "aphelion_unreachable_rate",
        "crosswalk_miss_rate",
        "circuit_open_count",
        "thresholds",
        "failures",
        "passed",
    ):
        assert key in payload, f"missing {key} in JSON report"


def test_threshold_override_honored(tmp_path: Path) -> None:
    """Even on a 5% diverge stream, exit 0 if the threshold is loosened to 10%."""
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(95)
    ]
    records.extend(
        _record(outcome="diverge", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(5)
    )
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
        "--threshold-discrepancy=0.10",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_now_anchor_is_deterministic(tmp_path: Path) -> None:
    """Same args + same --now → same rates."""
    records = [
        _record(outcome="diverge", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(10)
    ]
    _write(tmp_path, records)
    args = [
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=0",
        "--format=json",
        "--threshold-discrepancy=1.0",
        "--now=2026-04-26T12:00:00+00:00",
    ]
    a = json.loads(_run(*args).stdout)
    b = json.loads(_run(*args).stdout)
    assert a["discrepancy_rate"] == b["discrepancy_rate"]
    assert a["total_records"] == b["total_records"]


def test_human_format_output(tmp_path: Path) -> None:
    records = [_record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00")]
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 0
    assert "discrepancy_rate" in result.stdout
    assert "PASS" in result.stdout


# ---------------------------------------------------------------------------
# In-process coverage: import the script directly and exercise main() so
# coverage instrumentation captures the lines (subprocess invocation runs
# in a child interpreter outside the coverage tracer).
# ---------------------------------------------------------------------------


def _load_script_module():
    """Load ``scripts/dual_read_continuity_check.py`` as an importable module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_drc_inproc", str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_in_process_pass(tmp_path: Path, capsys) -> None:
    """Direct main() invocation → exit 0 on healthy stream."""
    drc = _load_script_module()
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(10)
    ]
    _write(tmp_path, records)
    rc = drc.main(
        [
            "--since=72h",
            f"--log-dir={tmp_path}",
            "--min-records=1",
            "--format=json",
            "--now=2026-04-26T12:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    payload = json.loads(captured.out)
    assert payload["passed"] is True
    assert payload["total_records"] == 10


def test_main_in_process_fail(tmp_path: Path, capsys) -> None:
    """Direct main() invocation → exit 1 on threshold breach."""
    drc = _load_script_module()
    records = [
        _record(outcome="diverge", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(10)
    ]
    _write(tmp_path, records)
    rc = drc.main(
        [
            "--since=72h",
            f"--log-dir={tmp_path}",
            "--min-records=1",
            "--format=json",
            "--now=2026-04-26T12:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1, captured.out
    payload = json.loads(captured.out)
    assert payload["passed"] is False
    assert "discrepancy_rate" in payload["failures"]


def test_main_in_process_human_format(tmp_path: Path, capsys) -> None:
    """Default human format prints PASS / threshold lines."""
    drc = _load_script_module()
    records = [_record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00")]
    _write(tmp_path, records)
    rc = drc.main(
        [
            "--since=72h",
            f"--log-dir={tmp_path}",
            "--min-records=1",
            "--now=2026-04-26T12:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "PASS" in captured.out
    assert "discrepancy_rate" in captured.out


def test_main_in_process_circuit_breach(tmp_path: Path, capsys) -> None:
    """Direct invocation exercises the circuit_open_count breach branch."""
    drc = _load_script_module()
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(96)
    ]
    records.extend(
        _record(
            outcome="match",
            timestamp="2026-04-26T11:30:00.000000+00:00",
            circuit_breaker_tripped=True,
        )
        for _ in range(4)
    )
    _write(tmp_path, records)
    rc = drc.main(
        [
            "--since=72h",
            f"--log-dir={tmp_path}",
            "--min-records=1",
            "--format=json",
            "--now=2026-04-26T12:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    payload = json.loads(captured.out)
    assert "circuit_open_count" in payload["failures"]


def test_main_in_process_aphelion_breach(tmp_path: Path, capsys) -> None:
    """Direct invocation exercises the aphelion_unreachable_rate breach branch."""
    drc = _load_script_module()
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(94)
    ]
    records.extend(
        _record(outcome="aphelion_unreachable", timestamp="2026-04-26T11:30:00.000000+00:00")
        for _ in range(6)
    )
    _write(tmp_path, records)
    rc = drc.main(
        [
            "--since=72h",
            f"--log-dir={tmp_path}",
            "--min-records=1",
            "--format=json",
            "--now=2026-04-26T12:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    payload = json.loads(captured.out)
    assert "aphelion_unreachable_rate" in payload["failures"]


def test_main_in_process_write_error_breach(tmp_path: Path, capsys) -> None:
    """Direct invocation exercises the write_error_rate breach branch."""
    drc = _load_script_module()
    records = [
        _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00") for _ in range(99)
    ]
    records.append(
        _record(
            outcome="match",
            timestamp="2026-04-26T11:30:00.000000+00:00",
            write_error_observed=True,
        )
    )
    _write(tmp_path, records)
    rc = drc.main(
        [
            "--since=72h",
            f"--log-dir={tmp_path}",
            "--min-records=1",
            "--format=json",
            "--now=2026-04-26T12:00:00+00:00",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    payload = json.loads(captured.out)
    assert "write_error_rate" in payload["failures"]


# ---------------------------------------------------------------------------
# H5 — distinguish missing dir from empty dir
# ---------------------------------------------------------------------------


def test_missing_log_dir_fails_by_default(tmp_path: Path) -> None:
    """Story H5 — pointing CLI at a nonexistent path fails by default."""
    missing = tmp_path / "does_not_exist"
    result = _run(
        "--since=72h",
        f"--log-dir={missing}",
        "--min-records=0",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["passed"] is False
    assert payload["log_dir_missing"] is True
    assert "log_dir_missing" in payload["failures"]


def test_missing_log_dir_with_allow_flag_passes(tmp_path: Path) -> None:
    """Story H5 — --allow-missing-dir downgrades the missing dir to OK."""
    missing = tmp_path / "does_not_exist"
    result = _run(
        "--since=72h",
        f"--log-dir={missing}",
        "--min-records=0",
        "--format=json",
        "--allow-missing-dir",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["log_dir_missing"] is True
    assert payload["passed"] is True
    assert "log_dir_missing" not in payload["failures"]


def test_empty_log_dir_distinguished_from_missing(tmp_path: Path) -> None:
    """Story H5 — an existing-but-empty dir reports log_dir_missing=False."""
    # tmp_path itself exists; just don't write any records.
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=0",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["log_dir_missing"] is False


# ---------------------------------------------------------------------------
# MED-MALFORMED-COUNTER — JSONL malformed line tracking
# ---------------------------------------------------------------------------


def test_cli_reports_malformed_count(tmp_path: Path) -> None:
    """3 valid + 2 malformed lines → JSON has ``malformed: 2``."""
    log_dir = tmp_path
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "dual-read-decisions-2026-04-26.jsonl"
    valid = _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00")
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(valid, sort_keys=True) + "\n")
        fh.write(json.dumps(valid, sort_keys=True) + "\n")
        fh.write("not-json-at-all\n")
        fh.write(json.dumps(valid, sort_keys=True) + "\n")
        fh.write("{still-broken}\n")
    result = _run(
        "--since=72h",
        f"--log-dir={log_dir}",
        "--min-records=0",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["total_records"] == 3
    assert payload["malformed"] == 2


# ---------------------------------------------------------------------------
# TEST-GAP-CONFLICT-RATE-BREACH — independent breach test for arbitration_conflict_rate
# ---------------------------------------------------------------------------


def test_arbitration_conflict_rate_breach_subprocess(tmp_path: Path) -> None:
    """TEST-GAP-CONFLICT-RATE-BREACH — >1% tie/fallback → exit 1 + breach.

    Synthesize a JSONL stream where >1% of dual-attempted entries have
    ``winning_source='tie' / 'fallback'``; run the CLI subprocess at default
    thresholds; assert exit 1 + 'arbitration_conflict_rate' in failures.
    """
    # 95 clean parallax wins + 5 ties → 5% conflict rate >> 1% threshold.
    records = []
    for _ in range(95):
        rec = _record(outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00")
        rec["winning_source"] = "parallax"
        records.append(rec)
    for _ in range(5):
        rec = _record(
            outcome="dual_attempted",
            timestamp="2026-04-26T11:30:00.000000+00:00",
        )
        rec["winning_source"] = "tie"
        records.append(rec)
    _write(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert "arbitration_conflict_rate" in payload["failures"]
