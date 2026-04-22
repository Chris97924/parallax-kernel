"""Shared FastAPI dependencies for the Parallax server.

Factors out the SQLite connection dependency so every route sees the same
lifecycle contract:

* open a fresh connection from :func:`parallax.config.load_config`
* yield it to the route
* close it on the way out, even on exception

App-level overrides (tests, in-memory DBs) are supported via the
``app.state.db_factory`` slot set by :func:`parallax.server.app.create_app`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator
from typing import cast

from fastapi import Request

from parallax.config import load_config
from parallax.sqlite_store import connect

__all__ = ["DBFactory", "get_conn", "default_db_factory"]


DBFactory = Callable[[], sqlite3.Connection]


def default_db_factory() -> sqlite3.Connection:
    """Open a connection from the current env config."""
    cfg = load_config()
    return connect(cfg.db_path)


def get_conn(request: Request) -> Generator[sqlite3.Connection, None, None]:
    """FastAPI dependency yielding a per-request SQLite connection.

    Connections are closed on teardown; tests swap out the factory on the
    app state to point at an in-memory / tmp DB. SQLite connections are not
    thread-safe, so we always create one per request rather than sharing.
    """
    factory = cast(
        DBFactory,
        getattr(request.app.state, "db_factory", default_db_factory),
    )
    conn = factory()
    try:
        yield conn
    finally:
        conn.close()
