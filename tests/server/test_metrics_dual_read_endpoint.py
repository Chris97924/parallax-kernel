"""Integration tests for the M3b dual-read gauges on ``GET /metrics``.

Story US-006-M3-T2.3: extend the existing ``/metrics`` endpoint with 4 new
Prometheus gauges + the ``parallax_arbitration_policy_version`` info-metric
without breaking the M2 shadow gauges.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from parallax.server.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build an app pointing at an isolated tmp shadow + dual-read log dir + DB."""
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("DUAL_READ_LOG_DIR", str(tmp_path / "dual_read"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(_REPO_ROOT / "parallax" / "schema.sql"))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dual_read").mkdir(parents=True, exist_ok=True)

    # Reset the metrics-route module cache so each test sees a fresh window.
    from parallax.server.routes import metrics as metrics_route

    metrics_route._reset_cache_for_tests()

    app = create_app()
    return TestClient(app)


def _write_dual_read(log_dir: Path, records: list[dict], date: str = "2026-04-26") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"dual-read-decisions-{date}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return path


def test_metrics_includes_dual_read_gauges(client: TestClient) -> None:
    """All 4 net-new dual-read gauges + policy_version info-metric present."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    for name in (
        "parallax_dual_read_discrepancy_rate",
        "parallax_arbitration_conflict_rate",
        "parallax_dual_read_write_error_rate",
        "parallax_arbitration_p99_latency_ms",
        "parallax_arbitration_policy_version",
    ):
        assert name in body, f"missing dual-read gauge {name} in /metrics output"


def test_metrics_preserves_m2_shadow_gauges(client: TestClient) -> None:
    """Story 6 must not regress the M2 shadow gauges already shipped."""
    resp = client.get("/metrics")
    body = resp.text
    for name in (
        "parallax_shadow_discrepancy_rate",
        "parallax_shadow_checksum_consistency",
        "parallax_shadow_log_records_total",
    ):
        assert name in body, f"M2 shadow gauge {name} regressed"


def test_metrics_policy_version_label_present(client: TestClient) -> None:
    """``parallax_arbitration_policy_version`` exposes a label with the RC string."""
    resp = client.get("/metrics")
    body = resp.text
    # Either as a label value or part of the help/value line — find the metric
    # line and assert the policy string is anywhere on it.
    found = False
    for line in body.splitlines():
        if line.startswith("parallax_arbitration_policy_version") and not line.startswith("#"):
            assert "v0.3.0-rc" in line, line
            found = True
    assert found, "parallax_arbitration_policy_version metric line not found"
