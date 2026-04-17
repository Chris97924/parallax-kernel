"""Shared pytest fixtures for the Parallax test suite."""

from __future__ import annotations

import pathlib
import sqlite3
from typing import Iterator

import pytest

from parallax.sqlite_store import connect

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """Fresh SQLite connection with the canonical schema applied."""
    db = tmp_path / "parallax.db"
    c = connect(db)
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        yield c
    finally:
        c.close()
