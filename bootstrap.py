"""Bootstrap a fresh Parallax instance at an arbitrary directory.

Creates ``<target>/db/`` and ``<target>/vault/`` and applies ``schema.sql`` to
``<target>/db/parallax.db``. Idempotent — the schema itself uses ``IF NOT
EXISTS``, so re-running is safe.

Usage:
    python bootstrap.py /path/to/new/parallax/instance
"""

from __future__ import annotations

import argparse
import pathlib

from parallax.config import ParallaxConfig
from parallax.sqlite_store import connect

_DEFAULT_SCHEMA = pathlib.Path(__file__).resolve().parent / "schema.sql"


def bootstrap(
    target_dir: pathlib.Path, schema_path: pathlib.Path | None = None
) -> ParallaxConfig:
    """Create the directory layout + DB for a new Parallax instance."""
    target_dir = pathlib.Path(target_dir).resolve()
    schema = (schema_path or _DEFAULT_SCHEMA).resolve()

    db_dir = target_dir / "db"
    vault_dir = target_dir / "vault"
    db_path = db_dir / "parallax.db"

    db_dir.mkdir(parents=True, exist_ok=True)
    vault_dir.mkdir(parents=True, exist_ok=True)

    sql = schema.read_text(encoding="utf-8")
    conn = connect(db_path)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()

    return ParallaxConfig(
        db_path=db_path.resolve(),
        vault_path=vault_dir.resolve(),
        schema_path=schema,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap a Parallax instance.")
    parser.add_argument("target_dir", type=pathlib.Path, help="Target directory")
    parser.add_argument(
        "--schema", type=pathlib.Path, default=None, help="Override schema.sql path"
    )
    args = parser.parse_args()
    cfg = bootstrap(args.target_dir, schema_path=args.schema)
    print(f"Parallax bootstrapped at: {cfg.db_path.parent.parent}")
    print(f"  db_path    = {cfg.db_path}")
    print(f"  vault_path = {cfg.vault_path}")
    print(f"  schema     = {cfg.schema_path}")


if __name__ == "__main__":
    main()
