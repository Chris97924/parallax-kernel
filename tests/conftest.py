"""Shared pytest fixtures for the Parallax test suite."""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """Fresh SQLite connection with all migrations applied."""
    db = tmp_path / "parallax.db"
    c = connect(db)
    migrate_to_latest(c)
    try:
        yield c
    finally:
        c.close()
