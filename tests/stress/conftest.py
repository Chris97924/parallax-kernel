"""Stress-test fixtures.

Reuses the canonical schema but returns a SQLite file path (not a connection)
so concurrency / fault-injection tests can open their own connections or
subprocess handles.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from parallax.sqlite_store import connect

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "schema.sql"


@pytest.fixture()
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a bootstrapped file-backed SQLite DB path."""
    db = tmp_path / "parallax_stress.db"
    c = connect(db)
    try:
        c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        c.close()
    return db


@pytest.fixture()
def conn(db_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    c = connect(db_path)
    try:
        yield c
    finally:
        c.close()
