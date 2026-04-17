"""RED-phase tests for parallax.config.

Contract:
    ParallaxConfig is a frozen dataclass with absolute pathlib.Path fields
    (db_path, vault_path, schema_path). load_config() reads env vars
    PARALLAX_DB_PATH / PARALLAX_VAULT_PATH / PARALLAX_SCHEMA_PATH and falls
    back to project-root defaults.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from parallax.config import ParallaxConfig, load_config


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


class TestParallaxConfig:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(ParallaxConfig)
        params = getattr(ParallaxConfig, "__dataclass_params__")
        assert params.frozen is True

    def test_has_expected_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(ParallaxConfig)}
        assert {"db_path", "vault_path", "schema_path"}.issubset(names)

    def test_cannot_mutate_fields(self, tmp_path: pathlib.Path) -> None:
        cfg = ParallaxConfig(
            db_path=tmp_path / "db.sqlite",
            vault_path=tmp_path / "vault",
            schema_path=tmp_path / "schema.sql",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.db_path = tmp_path / "other.sqlite"  # type: ignore[misc]


class TestLoadConfig:
    def test_defaults_are_absolute_under_project_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in ("PARALLAX_DB_PATH", "PARALLAX_VAULT_PATH", "PARALLAX_SCHEMA_PATH"):
            monkeypatch.delenv(key, raising=False)
        cfg = load_config()
        assert cfg.db_path.is_absolute()
        assert cfg.vault_path.is_absolute()
        assert cfg.schema_path.is_absolute()
        # Defaults point inside the project root
        assert PROJECT_ROOT in cfg.db_path.parents or cfg.db_path.parent == PROJECT_ROOT / "db"
        assert cfg.vault_path == (PROJECT_ROOT / "vault").resolve() or cfg.vault_path.parts[-1] == "vault"
        assert cfg.schema_path.name == "schema.sql"

    def test_env_overrides_applied(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "custom" / "x.db"
        vault = tmp_path / "custom" / "vault"
        schema = tmp_path / "custom" / "schema.sql"
        monkeypatch.setenv("PARALLAX_DB_PATH", str(db))
        monkeypatch.setenv("PARALLAX_VAULT_PATH", str(vault))
        monkeypatch.setenv("PARALLAX_SCHEMA_PATH", str(schema))
        cfg = load_config()
        assert cfg.db_path == db.resolve()
        assert cfg.vault_path == vault.resolve()
        assert cfg.schema_path == schema.resolve()

    def test_returns_parallax_config_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in ("PARALLAX_DB_PATH", "PARALLAX_VAULT_PATH", "PARALLAX_SCHEMA_PATH"):
            monkeypatch.delenv(key, raising=False)
        cfg = load_config()
        assert isinstance(cfg, ParallaxConfig)

    def test_relative_env_paths_become_absolute(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PARALLAX_DB_PATH", "relative/db.sqlite")
        monkeypatch.delenv("PARALLAX_VAULT_PATH", raising=False)
        monkeypatch.delenv("PARALLAX_SCHEMA_PATH", raising=False)
        cfg = load_config()
        assert cfg.db_path.is_absolute()
        assert cfg.db_path == (tmp_path / "relative" / "db.sqlite").resolve()
