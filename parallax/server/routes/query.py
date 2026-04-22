"""GET /query routes — progressive disclosure over :mod:`parallax.retrieve`.

``GET /query`` dispatches to one of the six v0.3.0 retrieval functions
based on the ``kind`` query parameter and returns L1/L2/L3 projections
(default L1). ``GET /query/reminder`` renders the
``<system-reminder>`` block produced by
:func:`parallax.injector.build_session_reminder` — this is the payload the
SessionStart hook plugin consumes.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from parallax import retrieve as R
from parallax.injector import build_session_reminder
from parallax.server.auth import current_user_id, require_auth
from parallax.server.deps import get_conn
from parallax.server.schemas import (
    RETRIEVE_KINDS,
    QueryResponse,
    ReminderResponse,
    RetrievalHitDTO,
    RetrieveKind,
)

router = APIRouter(
    prefix="/query",
    tags=["query"],
    dependencies=[Depends(require_auth)],
)


def _hit_to_dto(hit: R.RetrievalHit, *, level: int) -> RetrievalHitDTO:
    proj = hit.project(level)
    return RetrievalHitDTO(
        entity_kind=proj["entity_kind"],
        entity_id=proj["entity_id"],
        title=proj["title"],
        score=float(proj["score"]),
        level=level,  # type: ignore[arg-type]
        evidence=proj.get("evidence") if level >= 2 else None,
        full=_normalize_full(proj.get("full")) if level >= 3 else None,
        explain=hit.explain,
    )


def _normalize_full(full: Any) -> dict[str, Any] | str | None:
    """Ensure the L3 ``full`` field is JSON-serialisable.

    :meth:`parallax.retrieve.RetrievalHit.project` returns either a dict
    snapshot of the underlying row (from ``_event_to_hit`` / ``_claim_to_hit``)
    or the ``evidence`` string fallback when ``full is None``. Pydantic accepts
    both, but we coerce bytes/None defensively so the OpenAPI contract is
    predictable.
    """
    if full is None or isinstance(full, (dict, str)):
        return full
    return str(full)


def _dispatch(
    conn: sqlite3.Connection,
    *,
    kind: RetrieveKind,
    user_id: str,
    q: str,
    limit: int,
    since: str | None,
    until: str | None,
) -> list[R.RetrievalHit]:
    if kind == "recent":
        return R.recent_context(conn, user_id=user_id, limit=limit)
    if kind == "file":
        return R.by_file(conn, user_id=user_id, path=q, limit=limit)
    if kind == "decision":
        return R.by_decision(conn, user_id=user_id, limit=limit)
    if kind == "bug":
        return R.by_bug_fix(conn, user_id=user_id, limit=limit)
    if kind == "entity":
        return R.by_entity(conn, user_id=user_id, subject=q, limit=limit)
    # timeline
    if since is None or until is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="timeline kind requires 'since' and 'until' ISO-8601 params",
        )
    try:
        return R.by_timeline(
            conn, user_id=user_id, since=since, until=until, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get("", response_model=QueryResponse)
def get_query(
    request: Request,
    kind: RetrieveKind = Query(..., description=f"one of {list(RETRIEVE_KINDS)}"),
    user_id: str | None = Query(None, min_length=1, max_length=128),
    q: str = Query("", description="path / subject for file / entity kinds"),
    level: int = Query(1, ge=1, le=3, description="progressive disclosure tier"),
    limit: int = Query(10, ge=1, le=200),
    since: str | None = Query(None, description="ISO-8601 lower bound (timeline)"),
    until: str | None = Query(None, description="ISO-8601 upper bound (timeline)"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> QueryResponse:
    # Multi-user mode: the authenticated principal on request.state.user_id
    # overrides any query-string ``user_id`` (which is logged as a leak
    # attempt if it disagrees). Single-token mode: the query-string value
    # is required, same as before.
    resolved_user_id = current_user_id(request, user_id)
    hits = _dispatch(
        conn,
        kind=kind,
        user_id=resolved_user_id,
        q=q,
        limit=limit,
        since=since,
        until=until,
    )
    dtos = [_hit_to_dto(h, level=level) for h in hits]
    return QueryResponse(kind=kind, level=level, count=len(dtos), hits=dtos)


@router.get("/reminder", response_model=ReminderResponse)
def get_reminder(
    request: Request,
    user_id: str | None = Query(None, min_length=1, max_length=128),
    session_id: str | None = Query(None),
    max_hits: int = Query(8, ge=1, le=32),
    conn: sqlite3.Connection = Depends(get_conn),
) -> ReminderResponse:
    resolved_user_id = current_user_id(request, user_id)
    text = build_session_reminder(
        conn, user_id=resolved_user_id, session_id=session_id, max_hits=max_hits
    )
    return ReminderResponse(reminder=text, length=len(text))
