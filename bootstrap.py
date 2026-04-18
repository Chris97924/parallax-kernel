"""Bootstrap a fresh Parallax instance at an arbitrary directory.

Creates ``<target>/db/`` and ``<target>/vault/`` and runs every pending
migration via :func:`parallax.migrations.migrate_to_latest` against
``<target>/db/parallax.db``. Idempotent — already-applied migrations are
skipped, and migration 0001 uses ``CREATE IF NOT EXISTS`` so legacy DBs
that pre-date the migration framework upgrade cleanly.

Usage:
    python bootstrap.py /path/to/new/parallax/instance
"""

from __future__ import annotations

import argparse
import pathlib

from parallax.config import ParallaxConfig
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect

_DEFAULT_SCHEMA = pathlib.Path(__file__).resolve().parent / "schema.sql"


def bootstrap(
    target_dir: pathlib.Path, schema_path: pathlib.Path | None = None
) -> ParallaxConfig:
    """Create the directory layout + DB for a new Parallax instance.

    The ``schema_path`` argument is preserved for backward compatibility but
    is no longer the apply path: migrations under :mod:`parallax.migrations`
    own the DDL. The returned :class:`ParallaxConfig` still records the
    canonical ``schema.sql`` path as the human-readable SSoT.
    """
    target_dir = pathlib.Path(target_dir).resolve()
    schema = (schema_path or _DEFAULT_SCHEMA).resolve()

    db_dir = target_dir / "db"
    vault_dir = target_dir / "vault"
    db_path = db_dir / "parallax.db"

    db_dir.mkdir(parents=True, exist_ok=True)
    vault_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    try:
        migrate_to_latest(conn)
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
        "--schema",
        type=pathlib.Path,
        default=None,
        help="Override schema.sql path (recorded in ParallaxConfig only; "
        "migrations under parallax.migrations own the actual DDL).",
    )
    args = parser.parse_args()
    cfg = bootstrap(args.target_dir, schema_path=args.schema)
    print(f"Parallax bootstrapped at: {cfg.db_path.parent.parent}")
    print(f"  db_path    = {cfg.db_path}")
    print(f"  vault_path = {cfg.vault_path}")
    print(f"  schema     = {cfg.schema_path}")


if __name__ == "__main__":
    main()
