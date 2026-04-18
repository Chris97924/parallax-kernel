"""Tests for bootstrap.py — create a fresh Parallax instance at any path."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from bootstrap import bootstrap
from parallax.config import ParallaxConfig

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"
EXPECTED_TABLES = {
    "sources",
    "memories",
    "claims",
    "decisions",
    "events",
    "index_state",
    "schema_migrations",
    "claim_metadata",
}


class TestBootstrap:
    def test_returns_parallax_config(self, tmp_path: pathlib.Path) -> None:
        cfg = bootstrap(tmp_path)
        assert isinstance(cfg, ParallaxConfig)

    def test_creates_db_and_vault_dirs(self, tmp_path: pathlib.Path) -> None:
        bootstrap(tmp_path)
        assert (tmp_path / "db").is_dir()
        assert (tmp_path / "vault").is_dir()

    def test_applies_schema_and_creates_tables(self, tmp_path: pathlib.Path) -> None:
        cfg = bootstrap(tmp_path)
        assert cfg.db_path.is_file()
        with sqlite3.connect(str(cfg.db_path)) as c:
            rows = c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        names = {r[0] for r in rows}
        assert EXPECTED_TABLES.issubset(names)

    def test_idempotent_rerun(self, tmp_path: pathlib.Path) -> None:
        bootstrap(tmp_path)
        bootstrap(tmp_path)  # second run must not raise
        cfg = bootstrap(tmp_path)
        with sqlite3.connect(str(cfg.db_path)) as c:
            rows = c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        names = {r[0] for r in rows}
        assert EXPECTED_TABLES.issubset(names)

    def test_custom_schema_path_honored(self, tmp_path: pathlib.Path) -> None:
        cfg = bootstrap(tmp_path, schema_path=SCHEMA_PATH)
        assert cfg.schema_path == SCHEMA_PATH.resolve()

    def test_config_paths_point_under_target_dir(self, tmp_path: pathlib.Path) -> None:
        cfg = bootstrap(tmp_path)
        assert cfg.db_path == (tmp_path / "db" / "parallax.db").resolve()
        assert cfg.vault_path == (tmp_path / "vault").resolve()

    def test_bootstrap_runs_migrations(self, tmp_path: pathlib.Path) -> None:
        cfg = bootstrap(tmp_path)
        with sqlite3.connect(str(cfg.db_path)) as c:
            rows = c.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5]


class TestCLI:
    def test_cli_entry_creates_db(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import bootstrap as bootstrap_mod

        monkeypatch.setattr("sys.argv", ["bootstrap.py", str(tmp_path)])
        bootstrap_mod.main()
        assert (tmp_path / "db" / "parallax.db").is_file()
        assert (tmp_path / "vault").is_dir()
