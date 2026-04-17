"""Runtime configuration for Parallax.

A frozen ``ParallaxConfig`` dataclass holds the three filesystem paths the
rest of the package needs (DB, vault, schema). ``load_config()`` builds one
from the environment, falling back to project-root defaults so the package
is usable out of the box.

Environment variables
---------------------
PARALLAX_DB_PATH     Path to the SQLite database file.
PARALLAX_VAULT_PATH  Path to the markdown/vault root directory.
PARALLAX_SCHEMA_PATH Path to schema.sql (canonical DDL).

If a ``.env`` file is present at cwd or the project root, its values are
loaded first (best-effort — missing ``python-dotenv`` is not fatal).
"""

from __future__ import annotations

import dataclasses
import os
import pathlib

__all__ = ["ParallaxConfig", "load_config"]

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_DB = _PROJECT_ROOT / "db" / "parallax.db"
_DEFAULT_VAULT = _PROJECT_ROOT / "vault"
_DEFAULT_SCHEMA = _PROJECT_ROOT / "schema.sql"


@dataclasses.dataclass(frozen=True)
class ParallaxConfig:
    """Frozen snapshot of Parallax runtime paths."""

    db_path: pathlib.Path
    vault_path: pathlib.Path
    schema_path: pathlib.Path


def _maybe_load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (pathlib.Path.cwd() / ".env", _PROJECT_ROOT / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


def _resolve(env_key: str, default: pathlib.Path) -> pathlib.Path:
    raw = os.environ.get(env_key)
    return pathlib.Path(raw).resolve() if raw else default.resolve()


def load_config() -> ParallaxConfig:
    """Build a :class:`ParallaxConfig` from env vars + project defaults."""
    _maybe_load_dotenv()
    return ParallaxConfig(
        db_path=_resolve("PARALLAX_DB_PATH", _DEFAULT_DB),
        vault_path=_resolve("PARALLAX_VAULT_PATH", _DEFAULT_VAULT),
        schema_path=_resolve("PARALLAX_SCHEMA_PATH", _DEFAULT_SCHEMA),
    )
