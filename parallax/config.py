"""Runtime configuration for Parallax.

A frozen ``ParallaxConfig`` dataclass holds the filesystem paths and shadow
flags the rest of the package needs. ``load_config()`` builds one from the
environment, falling back to project-root defaults so the package is usable
out of the box.

Environment variables
---------------------
PARALLAX_DB_PATH        Path to the SQLite database file.
PARALLAX_VAULT_PATH     Path to the markdown/vault root directory.
PARALLAX_SCHEMA_PATH    Path to schema.sql (canonical DDL).
SHADOW_MODE             ``true``/``1``/``yes`` enables shadow observation
                        (read per-request by parallax.router.shadow for
                        hot-flip semantics — config is for /metrics + CLI).
SHADOW_USER_ALLOWLIST   Comma-separated user_id allowlist for shadow.
SHADOW_LOG_DIR          Directory holding shadow JSONL decision logs.

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
_DEFAULT_SHADOW_LOG_DIR = _PROJECT_ROOT / "parallax" / "logs"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclasses.dataclass(frozen=True)
class ParallaxConfig:
    """Frozen snapshot of Parallax runtime paths and shadow flags."""

    db_path: pathlib.Path
    vault_path: pathlib.Path
    schema_path: pathlib.Path
    shadow_mode: bool = False
    shadow_user_allowlist: tuple[str, ...] = ()
    shadow_log_dir: pathlib.Path = _DEFAULT_SHADOW_LOG_DIR


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


def _bool_env(env_key: str) -> bool:
    raw = os.environ.get(env_key, "").strip().lower()
    return raw in _TRUTHY


def _csv_env(env_key: str) -> tuple[str, ...]:
    raw = os.environ.get(env_key, "")
    return tuple(item for item in (chunk.strip() for chunk in raw.split(",")) if item)


def load_config() -> ParallaxConfig:
    """Build a :class:`ParallaxConfig` from env vars + project defaults."""
    _maybe_load_dotenv()
    return ParallaxConfig(
        db_path=_resolve("PARALLAX_DB_PATH", _DEFAULT_DB),
        vault_path=_resolve("PARALLAX_VAULT_PATH", _DEFAULT_VAULT),
        schema_path=_resolve("PARALLAX_SCHEMA_PATH", _DEFAULT_SCHEMA),
        shadow_mode=_bool_env("SHADOW_MODE"),
        shadow_user_allowlist=_csv_env("SHADOW_USER_ALLOWLIST"),
        shadow_log_dir=_resolve("SHADOW_LOG_DIR", _DEFAULT_SHADOW_LOG_DIR),
    )
