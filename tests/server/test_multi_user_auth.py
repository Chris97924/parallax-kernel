"""Multi-user auth tests (Story B3).

Covers:
    * migrate_to_latest creates the ``api_tokens`` table.
    * Helper inserts only the sha256 hash — plaintext never persists.
    * Single-token mode (default) still works exactly as v0.5.
    * Multi-user mode: valid token → user_id scoped; wrong / revoked /
      missing → 401.
    * CROSS-USER ISOLATION: a token for user A cannot read user B's
      claims (this is the safety-critical test).
    * Request-supplied user_id is ignored in multi-user mode — the
      authenticated principal wins.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from parallax.migrations import migrate_to_latest
from parallax.server import create_app
from parallax.server.auth import hash_token
from parallax.sqlite_store import connect, now_iso

# ---------- fixtures --------------------------------------------------------


@pytest.fixture()
def mu_db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "multi_user.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


@pytest.fixture()
def mu_app(
    mu_db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """App with multi-user auth enabled, pointed at the fresh tmp DB."""
    monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    monkeypatch.setenv("PARALLAX_DB_PATH", str(mu_db_path))

    def factory() -> sqlite3.Connection:
        return connect(mu_db_path)

    return create_app(db_factory=factory)


@pytest.fixture()
def mu_client(mu_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(mu_app) as c:
        yield c


def _create_token(db_path: pathlib.Path, *, user_id: str, label: str | None = None) -> str:
    """Mint a token for ``user_id``, write the hash, return the plaintext."""
    import secrets

    plaintext = secrets.token_urlsafe(24)
    token_hash = hash_token(plaintext)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO api_tokens(token_hash, user_id, created_at, "
            "revoked_at, label) VALUES (?, ?, ?, NULL, ?)",
            (token_hash, user_id, now_iso(), label),
        )
        conn.commit()
    finally:
        conn.close()
    return plaintext


def _revoke_token(db_path: pathlib.Path, *, plaintext: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE api_tokens SET revoked_at = ? WHERE token_hash = ?",
            (now_iso(), hash_token(plaintext)),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- schema + helper -------------------------------------------------


class TestApiTokensSchema:
    def test_migration_creates_table(self, mu_db_path: pathlib.Path) -> None:
        conn = connect(mu_db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_tokens'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_helper_stores_only_hash(self, mu_db_path: pathlib.Path) -> None:
        plaintext = _create_token(mu_db_path, user_id="alice", label="test")
        conn = connect(mu_db_path)
        try:
            rows = conn.execute(
                "SELECT token_hash, user_id, label FROM api_tokens WHERE user_id = ?",
                ("alice",),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        row = rows[0]
        # Row contains the sha256 hash, never the plaintext.
        assert row["token_hash"] == hash_token(plaintext)
        assert row["token_hash"] != plaintext
        assert row["user_id"] == "alice"
        assert row["label"] == "test"


# ---------- back-compat: single-token mode unchanged ------------------------


class TestSingleTokenModeUnchanged:
    def test_default_mode_still_open(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With PARALLAX_MULTI_USER unset and no token, open mode works."""
        # ``client`` fixture already clears PARALLAX_TOKEN; confirm open.
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        resp = client.post(
            "/ingest/memory",
            json={
                "user_id": "u",
                "title": "t",
                "summary": "s",
                "vault_path": "v.md",
            },
        )
        assert resp.status_code == 201

    def test_shared_secret_still_gates_requests(
        self, auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PARALLAX_TOKEN set, multi-user unset → v0.5 behavior."""
        monkeypatch.delenv("PARALLAX_MULTI_USER", raising=False)
        # Missing token
        r1 = auth_client.post(
            "/ingest/memory",
            json={"user_id": "u", "title": "t", "summary": "s", "vault_path": "v.md"},
        )
        assert r1.status_code == 401
        # Correct token
        r2 = auth_client.post(
            "/ingest/memory",
            json={"user_id": "u", "title": "t", "summary": "s", "vault_path": "v.md"},
            headers={"Authorization": "Bearer t0ken"},
        )
        assert r2.status_code == 201


# ---------- multi-user happy / error paths ----------------------------------


class TestMultiUserAuth:
    def test_valid_token_scopes_ingest(
        self, mu_client: TestClient, mu_db_path: pathlib.Path
    ) -> None:
        token = _create_token(mu_db_path, user_id="alice")
        resp = mu_client.post(
            "/ingest/memory",
            json={
                "user_id": "alice",  # matches authed user
                "title": "t",
                "summary": "s",
                "vault_path": "v.md",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201, resp.text
        # Response carries the authenticated user_id.
        assert resp.json()["user_id"] == "alice"

    def test_missing_bearer_401(self, mu_client: TestClient) -> None:
        resp = mu_client.post(
            "/ingest/memory",
            json={"user_id": "alice", "title": "t", "summary": "s", "vault_path": "v.md"},
        )
        assert resp.status_code == 401

    def test_unknown_token_401(self, mu_client: TestClient) -> None:
        resp = mu_client.post(
            "/ingest/memory",
            json={"user_id": "alice", "title": "t", "summary": "s", "vault_path": "v.md"},
            headers={"Authorization": "Bearer totally-unknown"},
        )
        assert resp.status_code == 401

    def test_revoked_token_401(
        self, mu_client: TestClient, mu_db_path: pathlib.Path
    ) -> None:
        token = _create_token(mu_db_path, user_id="alice")
        _revoke_token(mu_db_path, plaintext=token)
        resp = mu_client.post(
            "/ingest/memory",
            json={"user_id": "alice", "title": "t", "summary": "s", "vault_path": "v.md"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401

    def test_healthz_still_unauth(self, mu_client: TestClient) -> None:
        assert mu_client.get("/healthz").status_code == 200

    def test_missing_api_tokens_table_401(
        self, mu_client: TestClient, mu_db_path: pathlib.Path
    ) -> None:
        # Misconfigured deploy: multi-user mode enabled but migrations not
        # yet run. Must surface as 401, not a 500 leaking schema state.
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(mu_db_path))
        try:
            conn.execute("DROP TABLE IF EXISTS api_tokens")
            conn.commit()
        finally:
            conn.close()
        resp = mu_client.post(
            "/ingest/memory",
            json={"user_id": "alice", "title": "t", "summary": "s", "vault_path": "v.md"},
            headers={"Authorization": "Bearer any-token"},
        )
        assert resp.status_code == 401
        assert "invalid bearer token" in resp.text.lower()


# ---------- request-supplied user_id override -------------------------------


class TestAuthedUserIdOverrides:
    def test_body_user_id_ignored_when_authed(
        self, mu_client: TestClient, mu_db_path: pathlib.Path
    ) -> None:
        """Multi-user mode: a request claiming user_id=carol under Alice's
        token must still land in Alice's namespace."""
        alice_token = _create_token(mu_db_path, user_id="alice")
        resp = mu_client.post(
            "/ingest/memory",
            json={
                "user_id": "carol",  # spoof attempt
                "title": "t",
                "summary": "s",
                "vault_path": "v.md",
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 201
        assert resp.json()["user_id"] == "alice"

    def test_query_user_id_ignored_when_authed(
        self, mu_client: TestClient, mu_db_path: pathlib.Path
    ) -> None:
        """GET /query: a ?user_id=carol under Alice's token scopes to Alice."""
        alice_token = _create_token(mu_db_path, user_id="alice")
        # Seed Alice's claim via Alice's token (single source of truth).
        mu_client.post(
            "/ingest/claim",
            json={
                "user_id": "alice",
                "subject": "project-x",
                "predicate": "is",
                "object": "alice-only",
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        # Query with spoofed ?user_id=carol — should STILL see Alice's claim.
        resp = mu_client.get(
            "/query",
            params={"kind": "entity", "user_id": "carol", "q": "project-x"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1


# ---------- CROSS-USER ISOLATION (safety-critical) --------------------------


class TestCrossUserIsolation:
    def test_alice_cannot_see_bob_claims(
        self, mu_client: TestClient, mu_db_path: pathlib.Path
    ) -> None:
        alice_token = _create_token(mu_db_path, user_id="alice")
        bob_token = _create_token(mu_db_path, user_id="bob")

        # Alice ingests a claim.
        r_alice = mu_client.post(
            "/ingest/claim",
            json={
                "user_id": "alice",
                "subject": "secret-alice",
                "predicate": "is",
                "object": "top-secret",
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert r_alice.status_code == 201

        # Bob ingests his own claim.
        r_bob = mu_client.post(
            "/ingest/claim",
            json={
                "user_id": "bob",
                "subject": "secret-bob",
                "predicate": "is",
                "object": "bob-only",
            },
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        assert r_bob.status_code == 201

        # Bob queries Alice's subject under Bob's token — must return 0.
        resp = mu_client.get(
            "/query",
            params={"kind": "entity", "q": "secret-alice"},
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # No hit should surface Alice's claim for Bob.
        for hit in body["hits"]:
            assert "secret-alice" not in hit.get("title", "")
            assert "top-secret" not in (hit.get("evidence") or "")

        # Symmetry: Alice querying Bob's subject under Alice's token.
        resp2 = mu_client.get(
            "/query",
            params={"kind": "entity", "q": "secret-bob"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp2.status_code == 200
        for hit in resp2.json()["hits"]:
            assert "secret-bob" not in hit.get("title", "")
            assert "bob-only" not in (hit.get("evidence") or "")

        # Each user CAN see their own.
        resp3 = mu_client.get(
            "/query",
            params={"kind": "entity", "q": "secret-alice"},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert resp3.json()["count"] >= 1
