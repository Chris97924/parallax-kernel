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

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from parallax.server.app import create_app
from tests.shadow.conftest import make_record as _record
from tests.shadow.conftest import write_records as _write

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build an app pointing at an isolated tmp shadow log dir + DB."""
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(_REPO_ROOT / "parallax" / "schema.sql"))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)

    # Reset the metrics-route module cache so each test sees a fresh window.
    from parallax.server.routes import metrics as metrics_route

    metrics_route._reset_cache_for_tests()

    app = create_app()
    return TestClient(app)


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


# ---------------------------------------------------------------------------
# Drift guard: collapsed _collect_shadow_metrics must match public API
# ---------------------------------------------------------------------------


def test_metrics_collapsed_walk_matches_public_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_collect_shadow_metrics`` re-implements the public API for perf — semantics must match.

    This test pins the perf-collapsing optimization: if the metrics endpoint's
    in-memory aggregation drifts from ``discrepancy_rate()`` /
    ``checksum_consistency()``, scrapes silently report wrong numbers.
    """
    import datetime as dt

    from parallax.server.routes import metrics as metrics_route
    from parallax.shadow.discrepancy import (
        checksum_consistency,
        discrepancy_rate,
    )

    log_dir = tmp_path / "shadow"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SHADOW_LOG_DIR", str(log_dir))

    now = dt.datetime.now(dt.UTC)
    fresh = now.isoformat(timespec="microseconds")
    _write(
        log_dir,
        [
            _record(arbitration_outcome="match", timestamp=fresh),
            _record(arbitration_outcome="diverge", timestamp=fresh),
            _record(arbitration_outcome="match", timestamp=fresh),
        ],
        date=now.strftime("%Y-%m-%d"),
    )
    # Also append one malformed line so checksum_consistency drops below 1.0.
    path = log_dir / f"shadow-decisions-{now.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("garbage\n")

    metrics_route._reset_cache_for_tests()
    collapsed = metrics_route._collect_shadow_metrics()

    public_discrepancy = discrepancy_rate(window="1h")
    public_consistency = checksum_consistency(window="1h")

    assert collapsed["discrepancy_rate"] == pytest.approx(public_discrepancy)
    assert collapsed["checksum_consistency"] == pytest.approx(public_consistency)
    assert collapsed["log_records_total"] == 3.0  # 3 parsed records


# ---------------------------------------------------------------------------
# Metric name sanitization
# ---------------------------------------------------------------------------


def test_sanitize_metric_name_strips_label_selector() -> None:
    from parallax.server.routes.metrics import _sanitize_metric_name

    result = _sanitize_metric_name("parallax_deprecated_kind_total{kind='bug'}")
    assert result == "deprecated_kind_total"


def test_sanitize_metric_name_strips_prefix() -> None:
    from parallax.server.routes.metrics import _sanitize_metric_name

    assert _sanitize_metric_name("parallax_foo_total") == "foo_total"


def test_sanitize_metric_name_passthrough() -> None:
    from parallax.server.routes.metrics import _sanitize_metric_name

    assert _sanitize_metric_name("dedup_hit_total") == "dedup_hit_total"


def test_build_payload_sanitizes_registry_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter keys with embedded label selectors don't crash _build_payload()."""
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(_REPO_ROOT / "parallax" / "schema.sql"))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)

    from parallax.obs.metrics import get_counter, registry
    from parallax.server.routes import metrics as metrics_route

    metrics_route._reset_cache_for_tests()

    bad_key = "parallax_test_label_total{kind='x'}"
    get_counter(bad_key)
    try:
        payload = metrics_route._build_payload()
        assert "parallax_test_label_total" in payload
        assert "{" not in payload.split("parallax_test_label_total")[1].split("\n")[0]
    finally:
        registry.pop(bad_key, None)


def test_sanitize_metric_name_collapses_underscores() -> None:
    """Multi-character invalid runs collapse to a single ``_`` (no ``__``)."""
    from parallax.server.routes.metrics import _sanitize_metric_name

    # Three hyphens -> three "_" -> collapsed to one "_"
    assert _sanitize_metric_name("parallax_x---y") == "x_y"
    # Mixed invalid (space, dot, hyphen) -> single "_"
    assert _sanitize_metric_name("parallax_a b.c-d") == "a_b_c_d"


def test_sanitize_metric_name_handles_empty_after_strip() -> None:
    """Pure-prefix or pure-label inputs collapse to empty — caller must filter.

    Documents the boundary: ``_sanitize_metric_name`` does not invent a
    fallback name. ``_build_payload`` skips empty results — without that
    guard, ``Gauge('parallax_', ...)`` would silently register (the empty
    suffix is spec-valid as ``parallax_``), and a SECOND such key would
    crash the scrape with prometheus_client's duplicate-name check.
    """
    from parallax.server.routes.metrics import _sanitize_metric_name

    assert _sanitize_metric_name("parallax_") == ""
    assert _sanitize_metric_name("{kind='bug'}") == ""
    assert _sanitize_metric_name("___") == ""


def test_build_payload_skips_keys_that_collide_with_reserved_gauges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-house counter whose sanitized name collides with a reserved
    shadow gauge must NOT crash the scrape — the in-house loop skips it
    so the explicit shadow-gauge registration at the bottom of
    ``_build_payload`` doesn't trip ``Duplicated timeseries``.
    """
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(_REPO_ROOT / "parallax" / "schema.sql"))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)

    from parallax.obs.metrics import get_counter, registry
    from parallax.server.routes import metrics as metrics_route

    metrics_route._reset_cache_for_tests()

    # Pre-register inside try so a setup failure doesn't pollute the registry
    # for downstream tests (per python-reviewer + critic test-fixture nit).
    bad_keys = ["shadow_discrepancy_rate", "parallax_shadow_log_records_total"]
    try:
        for k in bad_keys:
            get_counter(k)
        # Must not raise; the explicit shadow gauge registration that follows
        # the in-house loop also must succeed (i.e. no duplicate name).
        payload = metrics_route._build_payload()
        # Reserved gauges still emit exactly once each (from the explicit
        # shadow-gauge block, not from the in-house mirror).
        for reserved in (
            "parallax_shadow_discrepancy_rate",
            "parallax_shadow_log_records_total",
        ):
            help_lines = [ln for ln in payload.splitlines() if ln.startswith(f"# HELP {reserved} ")]
            assert len(help_lines) == 1, f"{reserved} emitted {len(help_lines)}× (want 1)"
    finally:
        for k in bad_keys:
            registry.pop(k, None)


def test_build_payload_skips_keys_that_sanitize_to_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defends ``_build_payload`` against the empty-name latent crash path.

    Registers two pathological in-house keys that both collapse to the
    empty string after sanitization. Without the skip-guard at
    ``metrics.py``'s in-house-counter loop, the second registration would
    raise ``ValueError`` from prometheus_client (``Duplicated timeseries``)
    and 500 the scrape. With the guard, both are silently skipped and the
    scrape body still parses.
    """
    monkeypatch.setenv("SHADOW_LOG_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("PARALLAX_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(_REPO_ROOT / "parallax" / "schema.sql"))
    (tmp_path / "shadow").mkdir(parents=True, exist_ok=True)

    from parallax.obs.metrics import get_counter, registry
    from parallax.server.routes import metrics as metrics_route

    metrics_route._reset_cache_for_tests()

    bad_keys = ["parallax_", "{kind='only-label'}"]
    for k in bad_keys:
        get_counter(k)
    try:
        payload = metrics_route._build_payload()
        # No bare ``parallax_`` followed by space (HELP/TYPE/value) emitted.
        # Allow ``parallax_`` only as a substring of longer valid names.
        for line in payload.splitlines():
            if line.startswith("# HELP parallax_ ") or line.startswith("# TYPE parallax_ "):
                raise AssertionError(f"emitted bare parallax_ line: {line!r}")
            if line.startswith("parallax_ "):
                raise AssertionError(f"emitted bare parallax_ value line: {line!r}")
    finally:
        for k in bad_keys:
            registry.pop(k, None)
