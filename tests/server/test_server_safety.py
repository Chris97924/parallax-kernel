"""Production-safety guards introduced in v0.6.1.

Two boundaries are pinned here:

* ``/metrics`` is auth-gated whenever a token mode is configured, unless
  the operator explicitly opts in to ``PARALLAX_METRICS_PUBLIC=1``.
  Without these tests, the previous "always unauthenticated" behaviour
  could quietly leak ingest cadence + retrieve volume + shadow
  discrepancy rate to anyone who can reach the listener.
* ``assert_safe_to_start()`` refuses to construct the FastAPI app when
  ``PARALLAX_BIND_HOST`` is non-loopback and no auth is configured.
  This stops a misconfigured PM2 ecosystem file or `--host 0.0.0.0`
  from silently exposing an open kernel.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from parallax.migrations import migrate_to_latest
from parallax.server.app import create_app
from parallax.server.auth import assert_safe_to_start, bind_host_is_safe
from parallax.sqlite_store import connect


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "safety.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


def _make_app(db_path: pathlib.Path) -> FastAPI:
    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


# ---------------------------------------------------------------------------
# bind_host_is_safe primitive
# ---------------------------------------------------------------------------


class TestBindHostIsSafe:
    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "localhost", "::1", "[::1]", "", None, "LocalHost"]
    )
    def test_loopback_variants_all_safe(self, host: str | None) -> None:
        assert bind_host_is_safe(host) is True

    @pytest.mark.parametrize(
        "host", ["0.0.0.0", "192.168.1.111", "parallax.example.com", "::"]
    )
    def test_public_variants_all_unsafe(self, host: str) -> None:
        assert bind_host_is_safe(host) is False


# ---------------------------------------------------------------------------
# assert_safe_to_start
# ---------------------------------------------------------------------------


class TestAssertSafeToStart:
    def test_default_loopback_is_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PARALLAX_BIND_HOST", raising=False)
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        assert_safe_to_start()  # no raise

    def test_public_bind_with_token_is_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        assert_safe_to_start()

    def test_public_bind_with_multi_user_is_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        assert_safe_to_start()

    def test_public_bind_no_auth_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        with pytest.raises(RuntimeError, match="refusing to start"):
            assert_safe_to_start()

    def test_explicit_override_unblocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PARALLAX_ALLOW_OPEN_PUBLIC=1 is the documented escape hatch."""
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.setenv("PARALLAX_ALLOW_OPEN_PUBLIC", "1")
        assert_safe_to_start()

    def test_create_app_invokes_safety_check(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_ALLOW_OPEN_PUBLIC", raising=False)
        with pytest.raises(RuntimeError, match="refusing to start"):
            _make_app(db_path)


# ---------------------------------------------------------------------------
# /metrics auth posture — the route should fail closed when a token is set.
# ---------------------------------------------------------------------------


class TestMetricsAuthPosture:
    def test_metrics_open_when_no_auth(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_METRICS_PUBLIC", raising=False)

        app = _make_app(db_path)
        with TestClient(app) as client:
            resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_requires_auth_when_token_set(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_METRICS_PUBLIC", raising=False)

        app = _make_app(db_path)
        with TestClient(app) as client:
            anon = client.get("/metrics")
            authed = client.get(
                "/metrics", headers={"Authorization": "Bearer secret"}
            )
        assert anon.status_code == 401
        assert authed.status_code == 200

    def test_metrics_public_override_keeps_route_open_with_token(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.setenv("PARALLAX_METRICS_PUBLIC", "1")

        app = _make_app(db_path)
        with TestClient(app) as client:
            resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_wrong_token_rejected(
        self, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_TOKEN", "secret")
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        monkeypatch.delenv("PARALLAX_METRICS_PUBLIC", raising=False)

        app = _make_app(db_path)
        with TestClient(app) as client:
            resp = client.get(
                "/metrics", headers={"Authorization": "Bearer wrong"}
            )
        assert resp.status_code == 401
