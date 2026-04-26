"""WS-3 — ``GET /metrics`` Prometheus endpoint TDD coverage.

Contract:
- ``/metrics`` returns 200 with ``Content-Type: text/plain; version=0.0.4; charset=utf-8``
- Body is Prometheus text format (parses round-trip).
- Existing in-house counters from ``parallax.obs.metrics`` are exposed.
- Shadow gauges are exposed: ``parallax_shadow_discrepancy_rate``,
  ``parallax_shadow_checksum_consistency``, ``parallax_shadow_log_records_total``.
- The endpoint is unauthenticated (Prometheus scrape doesn't carry bearer tokens
  by default) — same posture as ``/healthz``.
- Gauge values reflect ``SHADOW_LOG_DIR`` contents at scrape time, with a 30s
  in-process cache so back-to-back scrapes don't re-walk disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from parallax.server.app import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build an app pointing at an isolated tmp shadow log dir + DB."""
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(Path("E:/Parallax/parallax/schema.sql")))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)

    # Reset the metrics-route module cache so each test sees a fresh window.
    from parallax.server.routes import metrics as metrics_route

    metrics_route._reset_cache_for_tests()

    app = create_app()
    return TestClient(app)


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


def _write(log_dir: Path, records: list[dict], date: str = "2026-04-26") -> None:
    path = log_dir / f"shadow-decisions-{date}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Endpoint shape
# ---------------------------------------------------------------------------


def test_metrics_returns_200_with_prometheus_content_type(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_metrics_includes_in_house_counters(client: TestClient) -> None:
    """Pre-registered parallax.obs.metrics counters surface as Prometheus counters."""
    resp = client.get("/metrics")
    body = resp.text
    for name in (
        "parallax_ingest_memory_total",
        "parallax_ingest_claim_total",
        "parallax_dedup_hit_total",
        "parallax_retrieve_total",
    ):
        assert name in body, f"missing in-house counter {name} in /metrics output"


def test_metrics_includes_shadow_gauges(client: TestClient) -> None:
    """Shadow observability gauges are present even when no shadow records exist."""
    resp = client.get("/metrics")
    body = resp.text
    for name in (
        "parallax_shadow_discrepancy_rate",
        "parallax_shadow_checksum_consistency",
        "parallax_shadow_log_records_total",
    ):
        assert name in body, f"missing shadow gauge {name} in /metrics output"


def test_metrics_unauthenticated_when_open_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No auth needed in open mode — /metrics is the Prometheus scrape target."""
    resp = client.get("/metrics")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Gauge values reflect log directory state
# ---------------------------------------------------------------------------


def test_discrepancy_rate_reflects_log_dir(client: TestClient, tmp_path: Path) -> None:
    """1/2 records diverge → /metrics reports parallax_shadow_discrepancy_rate 0.5."""
    log_dir = tmp_path / "shadow"
    # Use a recent timestamp so the rolling 1h window catches it
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    fresh = now.isoformat(timespec="microseconds")
    _write(
        log_dir,
        [
            _record(arbitration_outcome="match", timestamp=fresh),
            _record(arbitration_outcome="diverge", timestamp=fresh),
        ],
        date=now.strftime("%Y-%m-%d"),
    )

    resp = client.get("/metrics")
    body = resp.text
    # Find the metric line: 'parallax_shadow_discrepancy_rate 0.5'
    found = False
    for line in body.splitlines():
        if line.startswith("parallax_shadow_discrepancy_rate ") and not line.startswith("#"):
            value = float(line.split()[1])
            assert abs(value - 0.5) < 1e-9, line
            found = True
            break
    assert found, "expected parallax_shadow_discrepancy_rate metric line"


def test_log_records_total_counts_in_window(client: TestClient, tmp_path: Path) -> None:
    """parallax_shadow_log_records_total reports parsed records in the rolling window."""
    log_dir = tmp_path / "shadow"
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    fresh = now.isoformat(timespec="microseconds")
    _write(
        log_dir,
        [_record(timestamp=fresh) for _ in range(7)],
        date=now.strftime("%Y-%m-%d"),
    )

    resp = client.get("/metrics")
    found = False
    for line in resp.text.splitlines():
        if line.startswith("parallax_shadow_log_records_total ") and not line.startswith("#"):
            value = float(line.split()[1])
            assert value == 7, line
            found = True
            break
    assert found, "expected parallax_shadow_log_records_total metric line"


def test_metrics_caches_within_ttl(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-to-back scrapes don't re-read disk; second scrape uses cache."""

    log_dir = tmp_path / "shadow"
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    _write(
        log_dir,
        [_record(timestamp=now.isoformat(timespec="microseconds"))],
        date=now.strftime("%Y-%m-%d"),
    )

    # First scrape populates cache.
    client.get("/metrics")

    # Mutate disk: append another record. Cache should mask it.
    _write(
        log_dir,
        [_record(timestamp=now.isoformat(timespec="microseconds"))],
        date=now.strftime("%Y-%m-%d"),
    )

    resp = client.get("/metrics")
    for line in resp.text.splitlines():
        if line.startswith("parallax_shadow_log_records_total ") and not line.startswith("#"):
            value = float(line.split()[1])
            # If cache is working, value stays at 1; if broken, it would be 2.
            assert value == 1, f"cache miss — saw {value} (expected cached 1)"
            return
    pytest.fail("missing parallax_shadow_log_records_total metric line")
