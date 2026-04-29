"""M3-T1.5 — Integration tests for POST /admin/circuit_breaker/reset (US-011).

Covers:
1. Without bearer token → 401.
2. With valid auth, breaker was tripped → resets breaker, was_tripped=True.
3. With valid auth, breaker was NOT tripped → was_tripped=False.
4. No os.environ mutation across the reset call.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from parallax.migrations import migrate_to_latest
from parallax.router.circuit_breaker import get_breaker_state
from parallax.server.app import create_app
from parallax.sqlite_store import connect

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOKEN = "admin-test-token"


def _trip_breaker() -> None:
    """Push enough observations to trip the singleton breaker."""
    state = get_breaker_state()
    # 200 obs, 50 unreachable (25% >> 1%)
    for i in range(200):
        state.record_unreachable_observation(observed_unreachable=(i < 50))
    assert state.is_tripped() is True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "admin_test.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


@pytest.fixture()
def auth_app(db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PARALLAX_TOKEN", _TOKEN)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


@pytest.fixture()
def open_app(db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """App in open mode (no auth)."""
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


@pytest.fixture(autouse=True)
def _clean_breaker():
    """Ensure singleton is reset before/after each test."""
    get_breaker_state().reset()
    yield
    get_breaker_state().reset()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_reset_requires_auth(auth_app):
    """Without a bearer token the endpoint must reject the request (401)."""
    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post("/admin/circuit_breaker/reset")
    assert resp.status_code == 401


@pytest.mark.integration
def test_post_reset_clears_breaker(auth_app):
    """With valid auth and a tripped breaker: response is ok + was_tripped=True."""
    _trip_breaker()

    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/admin/circuit_breaker/reset",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["was_tripped"] is True
    assert "reset_at" in body

    # Singleton must now be untripped
    assert get_breaker_state().is_tripped() is False


@pytest.mark.integration
def test_post_reset_when_not_tripped(auth_app):
    """Fresh breaker → was_tripped=False in response, state unchanged."""
    assert get_breaker_state().is_tripped() is False

    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/admin/circuit_breaker/reset",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["was_tripped"] is False
    assert get_breaker_state().is_tripped() is False


@pytest.mark.integration
def test_post_reset_emits_no_state_mutation_outside_singleton(auth_app):
    """The reset endpoint must not mutate os.environ."""
    before = dict(os.environ)

    with TestClient(auth_app, raise_server_exceptions=False) as client:
        client.post(
            "/admin/circuit_breaker/reset",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    after = dict(os.environ)
    assert before == after, (
        f"os.environ was mutated. "
        f"added={set(after) - set(before)}, "
        f"removed={set(before) - set(after)}"
    )


# ---------------------------------------------------------------------------
# US-006: structured audit log on every reset (tripped + idempotent paths)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_reset_emits_audit_warning_on_tripped_breaker(
    auth_app, monkeypatch: pytest.MonkeyPatch
):
    """Every reset must emit a structured WARNING for audit, regardless of
    whether the breaker was tripped at call time. Captures via direct
    monkeypatch since Parallax's structured logger has propagate=False.
    """
    # Trip the breaker first.
    state = get_breaker_state()
    for _ in range(60):
        state.record_unreachable_observation(observed_unreachable=True)
    assert state.is_tripped()

    import parallax.server.routes.admin.circuit_breaker as admin_mod

    audit_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        admin_mod._log,
        "warning",
        lambda msg, *a, **kw: audit_calls.append((msg, dict(kw))),
    )

    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/admin/circuit_breaker/reset",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status_code == 200
    assert any(
        msg == "circuit_breaker.reset.invoked" and kw.get("extra", {}).get("was_tripped") is True
        for msg, kw in audit_calls
    ), f"Expected audit WARNING with was_tripped=True; got {audit_calls}"


@pytest.mark.integration
def test_post_reset_emits_audit_warning_on_idempotent_no_op(
    auth_app, monkeypatch: pytest.MonkeyPatch
):
    """Audit log fires even when the reset is a no-op (breaker already
    untripped) — token-spam attacks must be observable regardless."""
    assert get_breaker_state().is_tripped() is False

    import parallax.server.routes.admin.circuit_breaker as admin_mod

    audit_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        admin_mod._log,
        "warning",
        lambda msg, *a, **kw: audit_calls.append((msg, dict(kw))),
    )

    with TestClient(auth_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/admin/circuit_breaker/reset",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert resp.status_code == 200
    assert any(
        msg == "circuit_breaker.reset.invoked" and kw.get("extra", {}).get("was_tripped") is False
        for msg, kw in audit_calls
    ), f"Expected audit WARNING with was_tripped=False; got {audit_calls}"
