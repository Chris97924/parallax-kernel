"""TDD coverage for ``scripts/shadow_continuity_check.py``.

Contract:
- ``--since=72h`` (default), ``--log-dir=PATH``, ``--threshold-discrepancy=N``,
  ``--threshold-checksum=N``, ``--format={human,json}``, ``--min-records=N``.
- Exit code 0 iff all DoD thresholds met AND record count >= ``--min-records``.
- Exit code 1 otherwise. Stderr never used for normal output.
- Reports: total records, malformed lines, divergent records, discrepancy_rate,
  checksum_consistency, final chain hash, window boundaries.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "shadow_continuity_check.py"


def _record(
    arbitration_outcome: str = "match",
    timestamp: str = "2026-04-26T11:00:00.000000+00:00",
    **overrides: Any,
) -> dict[str, Any]:
    base = {
        "arbitration_outcome": arbitration_outcome,
        "correlation_id": "cid-1",
        "crosswalk_status": "ok",
        "latency_ms": 1.0,
        "query_type": "recent_context",
        "schema_version": "1.0",
        "selected_port": "QueryPort",
        "timestamp": timestamp,
        "user_id": "alice",
    }
    base.update(overrides)
    return base


def _write_records(log_dir: Path, records: list[dict], date: str = "2026-04-26") -> Path:
    path = log_dir / f"shadow-decisions-{date}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return path


def _run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Invoke the CLI with the project's interpreter; capture stdout+stderr."""
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


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"missing CLI: {SCRIPT}"


def test_help_runs(tmp_path: Path) -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "shadow_continuity_check" in result.stdout or "since" in result.stdout


def test_empty_log_dir_min_records_fails(tmp_path: Path) -> None:
    """Empty log dir + min-records=1 → exit 1 (no shadow activity = DoD fail)."""
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["total_records"] == 0
    assert payload["passed"] is False
    assert "min_records" in payload["failures"]


def test_all_match_passes_dod(tmp_path: Path) -> None:
    """1000 match records, no malformed → DoD passes, exit 0."""
    records = [
        _record(timestamp=f"2026-04-26T11:{i // 60:02d}:{i % 60:02d}.000000+00:00")
        for i in range(1000)
    ]
    _write_records(tmp_path, records)
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
    assert payload["divergent"] == 0
    assert payload["malformed"] == 0
    assert payload["discrepancy_rate"] == 0.0
    assert payload["checksum_consistency"] == 1.0
    assert payload["passed"] is True


def test_high_discrepancy_fails(tmp_path: Path) -> None:
    """5/100 diverge = 5% > 0.3% threshold → exit 1."""
    records = [
        _record(arbitration_outcome="match", timestamp="2026-04-26T11:30:00.000000+00:00")
        for _ in range(95)
    ]
    records.extend(
        _record(arbitration_outcome="diverge", timestamp="2026-04-26T11:30:00.000000+00:00")
        for _ in range(5)
    )
    _write_records(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["divergent"] == 5
    assert payload["discrepancy_rate"] == pytest.approx(0.05)
    assert "discrepancy_rate" in payload["failures"]


def test_malformed_line_fails_checksum(tmp_path: Path) -> None:
    """Below-threshold checksum consistency → exit 1."""
    records = [_record(timestamp="2026-04-26T11:00:00.000000+00:00")]
    path = _write_records(tmp_path, records)
    with path.open("a", encoding="utf-8") as fh:
        # 1 valid + 5 malformed = 1/6 consistent ~ 0.167 << 0.999
        for _ in range(5):
            fh.write("garbage\n")
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["malformed"] == 5
    assert payload["checksum_consistency"] < 0.999
    assert "checksum_consistency" in payload["failures"]


def test_chain_hash_deterministic(tmp_path: Path) -> None:
    """Same input → same final chain hash on repeat runs."""
    records = [
        _record(timestamp="2026-04-26T11:00:00.000000+00:00", correlation_id=f"c{i}")
        for i in range(3)
    ]
    _write_records(tmp_path, records)

    args = [
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
    ]
    a = json.loads(_run(*args).stdout)
    b = json.loads(_run(*args).stdout)
    assert a["chain_hash"] == b["chain_hash"]
    assert len(a["chain_hash"]) == 64  # SHA-256 hex


def test_human_format_output(tmp_path: Path) -> None:
    """Default human format prints headline numbers without JSON envelope."""
    records = [_record(timestamp="2026-04-26T11:00:00.000000+00:00")]
    _write_records(tmp_path, records)
    result = _run(
        "--since=72h",
        f"--log-dir={tmp_path}",
        "--min-records=1",
        "--now=2026-04-26T12:00:00+00:00",
    )
    assert result.returncode == 0
    assert "discrepancy_rate" in result.stdout
    assert "checksum_consistency" in result.stdout
    assert "PASS" in result.stdout


def test_log_dir_defaults_to_env(tmp_path: Path) -> None:
    """Without --log-dir, fall back to SHADOW_LOG_DIR."""
    records = [_record(timestamp="2026-04-26T11:00:00.000000+00:00")]
    _write_records(tmp_path, records)
    result = _run(
        "--since=72h",
        "--min-records=1",
        "--format=json",
        "--now=2026-04-26T12:00:00+00:00",
        env_extra={"SHADOW_LOG_DIR": str(tmp_path)},
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["total_records"] == 1
