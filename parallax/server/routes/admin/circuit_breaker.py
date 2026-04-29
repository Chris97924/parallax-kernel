"""M3-T1.5 — Admin endpoint for manual circuit-breaker reset (US-011).

POST /admin/circuit_breaker/reset
    Operator-only manual re-arm.  Run AFTER verifying Aphelion health.

Auth: requires the standard bearer token (same as all other non-/healthz routes).
No env mutation.  Returns JSON with was_tripped + reset_at.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from parallax.obs.log import get_logger
from parallax.router.circuit_breaker import get_breaker_state
from parallax.server.auth import require_auth

__all__ = ["router"]

_log = get_logger("parallax.server.routes.admin.circuit_breaker")

router = APIRouter(prefix="/admin/circuit_breaker", tags=["admin"])


@router.post("/reset", dependencies=[Depends(require_auth)])
async def reset_circuit_breaker() -> dict:
    """Operator-only manual re-arm.

    Run ONLY after verifying Aphelion health — the breaker does NOT
    auto-recover (Q10 DECIDED, ralplan §10 line 552).

    Every reset emits a structured WARNING with ``was_tripped`` +
    ``reset_at`` so a leaked bearer-token attempt to suppress an outage
    by spamming reset is observable in logs.

    Returns
    -------
    ok : bool
        Always True on success.
    was_tripped : bool
        Whether the breaker was tripped before this reset.
    reset_at : str
        ISO-8601 UTC timestamp of the reset.
    """
    state = get_breaker_state()
    was_tripped = state.is_tripped()
    reset_at = datetime.now(UTC).isoformat()
    # Audit BEFORE the action so the operator's intent is recorded even if
    # ``state.reset()`` raises.  The log call itself is wrapped so a closed
    # logger handler cannot silently block the reset.
    try:
        _log.warning(
            "circuit_breaker.reset.invoked",
            extra={
                "event": "circuit_breaker.reset.invoked",
                "was_tripped": was_tripped,
                "reset_at": reset_at,
            },
        )
    except Exception:  # noqa: BLE001 — last-resort: never block reset on logger failure
        pass
    state.reset()
    return {
        "ok": True,
        "was_tripped": was_tripped,
        "reset_at": reset_at,
    }
