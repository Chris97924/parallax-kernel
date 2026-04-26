"""GET /inspect routes — health + introspection for operators.

Two endpoints:

* ``GET /inspect/health`` — wraps :func:`parallax.telemetry.health`. Marks the
  instance ``degraded`` when a last_error is recorded or WAL isn't enabled,
  otherwise ``ok``. Unauthenticated callers receive ok-only payload; bearer
  auth returns full HealthResponse.
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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from parallax.introspection import parallax_info
from parallax.router import RealMemoryRouter, is_router_enabled
from parallax.server.auth import auth_configured, multi_user_mode, require_auth
from parallax.server.deps import get_conn
from parallax.server.schemas import HealthOkResponse, HealthResponse, InspectResponse
from parallax.telemetry import health as telemetry_health

_optional_bearer = HTTPBearer(auto_error=False)
_CONN_DEP = Depends(get_conn)
_OPTIONAL_BEARER_DEP = Depends(_optional_bearer)

router = APIRouter(
    prefix="/inspect",
    tags=["inspect"],
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


def _build_health_with_router(conn: sqlite3.Connection) -> HealthResponse:
    """Compute HealthResponse, optionally merging router liveness.

    When MEMORY_ROUTER is enabled, the router's health report is merged
    into the response — if the router reports not-ok, the status is degraded.
    US-D3-03 requirement; no module-level caching to avoid cross-request
    state contamination in tests and multi-factory setups.
    """
    result = _build_health(conn)
    if is_router_enabled():
        router_report = RealMemoryRouter(conn).health()
        if not router_report.ok:
            result = HealthResponse(
                status="degraded",
                db_path=result.db_path,
                journal_mode=result.journal_mode,
                table_counts=result.table_counts,
                last_error="router_unhealthy",
            )
    return result


@router.get("/health", response_model=None)
def get_health(
    request: Request,
    conn: sqlite3.Connection = _CONN_DEP,
    credentials: HTTPAuthorizationCredentials | None = _OPTIONAL_BEARER_DEP,
) -> HealthResponse | HealthOkResponse:
    health = _build_health_with_router(conn)
    # H-2: redact full payload when auth is required but caller is not authenticated.
    # Covers single-token mode (PARALLAX_TOKEN) and multi-user mode. Open mode
    # (neither env var set) returns the full payload to all callers.
    if auth_configured() or multi_user_mode():
        try:
            require_auth(request, credentials, conn)
        except HTTPException:
            return HealthOkResponse(status=health.status)
    return health


@router.get("/info", response_model=InspectResponse, dependencies=[Depends(require_auth)])
def get_info(
    conn: sqlite3.Connection = _CONN_DEP,
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
