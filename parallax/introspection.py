"""parallax_info() — at-a-glance snapshot of a Parallax instance."""

from __future__ import annotations

import dataclasses
import pathlib

from parallax.sqlite_store import connect, query

__all__ = ["ParallaxInfo", "parallax_info"]


@dataclasses.dataclass(frozen=True)
class ParallaxInfo:
    version: str
    db_path: str
    schema_version: int | None
    memories_count: int
    claims_count: int
    sources_count: int
    events_count: int


def _count(conn, table: str) -> int:
    rows = query(conn, f"SELECT COUNT(*) AS n FROM {table}")
    return int(rows[0]["n"])


def _schema_version(conn) -> int | None:
    tables = query(
        conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    )
    if not tables:
        return None
    rows = query(
        conn, "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    )
    return rows[0]["version"] if rows else None


def parallax_info(db_path: pathlib.Path | str) -> ParallaxInfo:
    from parallax import __version__

    conn = connect(db_path)
    try:
        return ParallaxInfo(
            version=__version__,
            db_path=str(pathlib.Path(db_path).resolve()),
            schema_version=_schema_version(conn),
            memories_count=_count(conn, "memories"),
            claims_count=_count(conn, "claims"),
            sources_count=_count(conn, "sources"),
            events_count=_count(conn, "events"),
        )
    finally:
        conn.close()
