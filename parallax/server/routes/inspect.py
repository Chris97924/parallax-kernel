"""GET /inspect routes — health + introspection for operators.

Two endpoints:

* ``GET /inspect/health`` — wraps :func:`parallax.telemetry.health`. Marks the
  instance ``degraded`` when a last_error is recorded or WAL isn't enabled,
  otherwise ``ok``.
* ``GET /inspect/info`` — wraps :func:`parallax.introspection.parallax_info`
  and folds the health payload into the response so dashboards only need
  one round-trip.

Both routes use :func:`parallax.server.deps.get_conn` so the app-level
``db_factory`` override (used by tests and the local/HTTP mode switch)
governs which DB they read, instead of duplicating ``load_config()`` here.
The absolute DB path is redacted to its basename before being returned,
so the wire response never leaks the host filesystem layout.
"""

from __future__ import annotations

import pathlib
import sqlite3

from fastapi import APIRouter, Depends

from parallax.introspection import parallax_info
from parallax.server.auth import require_auth
from parallax.server.deps import get_conn
from parallax.server.schemas import HealthResponse, InspectResponse
from parallax.telemetry import health as telemetry_health

router = APIRouter(
    prefix="/inspect",
    tags=["inspect"],
    dependencies=[Depends(require_auth)],
)


def _db_path_from_conn(conn: sqlite3.Connection) -> str:
    """Ask SQLite where 'main' lives. Empty string for in-memory DBs."""
    for _seq, name, path in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return str(path or "")
    return ""


def _redact(path: str) -> str:
    """Return only the filename — never the absolute host path."""
    if not path:
        return ""
    return pathlib.PurePath(path).name


def _build_health(conn: sqlite3.Connection) -> HealthResponse:
    db_path = _db_path_from_conn(conn)
    raw = telemetry_health(db_path)
    degraded = bool(raw.get("last_error")) or raw.get("journal_mode") != "wal"
    return HealthResponse(
        status="degraded" if degraded else "ok",
        db_path=_redact(str(raw["db_path"])),
        journal_mode=str(raw.get("journal_mode", "")),
        table_counts=dict(raw.get("table_counts") or {}),
        last_error=raw.get("last_error"),
    )


@router.get("/health", response_model=HealthResponse)
def get_health(
    conn: sqlite3.Connection = Depends(get_conn),
) -> HealthResponse:
    return _build_health(conn)


@router.get("/info", response_model=InspectResponse)
def get_info(
    conn: sqlite3.Connection = Depends(get_conn),
) -> InspectResponse:
    db_path = _db_path_from_conn(conn)
    info = parallax_info(db_path)
    return InspectResponse(
        version=info.version,
        db_path=_redact(info.db_path),
        schema_version=info.schema_version,
        memories_count=info.memories_count,
        claims_count=info.claims_count,
        sources_count=info.sources_count,
        events_count=info.events_count,
        health=_build_health(conn),
    )
