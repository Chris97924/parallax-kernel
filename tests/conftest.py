"""Shared pytest fixtures for the Parallax test suite."""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from collections.abc import Iterator

import pytest

from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect


def pytest_sessionstart(session: pytest.Session) -> None:
    """Lower cov-fail-under when running only test_regenerate.py.

    test_regenerate.py covers an external script outside the parallax
    package, so it contributes zero lines to the parallax coverage total.
    Running it in isolation would always fail the 80% gate.

    pytest-cov reads ``CovPlugin.options.cov_fail_under`` (not
    ``config.option``) at ``pytest_terminal_summary`` time, so we reach
    into the registered plugin instance and zero it out.
    """
    args = sys.argv[1:]
    regen_only = any("test_regenerate" in a for a in args) and all(
        "test_regenerate" in a or a.startswith("-") for a in args
    )
    if not regen_only:
        return
    # Patch config.option (used by some pytest-cov paths)
    try:
        session.config.option.cov_fail_under = 0.0
    except AttributeError:
        pass
    # Patch the CovPlugin instance directly (used by pytest_terminal_summary)
    plugin = session.config.pluginmanager.get_plugin("_cov")
    if plugin is not None:
        try:
            plugin.options.cov_fail_under = 0.0
        except AttributeError:
            pass


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
