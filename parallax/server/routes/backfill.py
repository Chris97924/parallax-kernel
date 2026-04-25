"""POST /backfill — flag-gated crosswalk backfill via RealMemoryRouter."""
from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from parallax.router import RealMemoryRouter, is_router_enabled
from parallax.router.contracts import BackfillRequest
from parallax.server.auth import current_user_id, require_auth
from parallax.server.deps import get_conn
from parallax.server.schemas import (
    ArbitrationDecisionDTO,
    BackfillBodyRequest,
    BackfillReportResponse,
)

router = APIRouter(
    prefix="/backfill",
    tags=["backfill"],
    dependencies=[Depends(require_auth)],
)


@router.post("", response_model=BackfillReportResponse)
def post_backfill(
    body: BackfillBodyRequest,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> BackfillReportResponse:
    if not is_router_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MEMORY_ROUTER is not enabled; set MEMORY_ROUTER=true to use /backfill",
        )
    user_id = current_user_id(request, body.user_id)
    req = BackfillRequest(
        user_id=user_id,
        crosswalk_version=body.crosswalk_version,
        dry_run=body.dry_run,
        scope=body.scope,
    )
    try:
        report = RealMemoryRouter(conn).backfill(req)
    except (ValueError, RuntimeError) as exc:
        code = (
            status.HTTP_400_BAD_REQUEST
            if isinstance(exc, ValueError)
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return BackfillReportResponse(
        rows_examined=report.rows_examined,
        rows_mapped=report.rows_mapped,
        rows_unmapped=report.rows_unmapped,
        rows_conflict=report.rows_conflict,
        writes_performed=report.writes_performed,
        arbitrations=[
            ArbitrationDecisionDTO(
                canonical_field=a.canonical_field,
                state=a.state.value,
                reason_code=a.reason_code,
                reason=a.reason,
                confidence=a.confidence,
                requires_manual_review=a.requires_manual_review,
            )
            for a in report.arbitrations
        ],
    )
