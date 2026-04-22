"""Fixtures for the Parallax HTTP server test suite.

Each test gets a fresh on-disk SQLite DB under ``tmp_path`` (in-memory DBs
don't play well with the per-request ``connect()`` pattern the server
uses — each new connection would see an empty DB). We build a FastAPI app
via the ``create_app`` factory and override its ``db_factory`` to point at
the tmp DB, so the server code paths execute unchanged.
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
from parallax.sqlite_store import connect


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Fresh migrated DB under tmp_path. Returned as an absolute path."""
    p = tmp_path / "server.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


@pytest.fixture()
def app(db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """App wired to the tmp DB, no auth (open mode)."""
    monkeypatch.delenv("PARALLAX_TOKEN", raising=False)
    # inspect routes call telemetry.health + parallax_info via load_config();
    # point those at the tmp DB too.
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_app(
    db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> FastAPI:
    """App with PARALLAX_TOKEN set — exercises auth-enabled paths."""
    monkeypatch.setenv("PARALLAX_TOKEN", "t0ken")
    monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))

    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


@pytest.fixture()
def auth_client(auth_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(auth_app) as c:
        yield c
