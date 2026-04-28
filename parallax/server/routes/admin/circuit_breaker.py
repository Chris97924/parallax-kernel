"""M3-T1.5 — Admin endpoint for manual circuit-breaker reset (US-011).

POST /admin/circuit_breaker/reset
    Operator-only manual re-arm.  Run AFTER verifying Aphelion health.

Auth: requires the standard bearer token (same as all other non-/healthz routes).
No env mutation.  Returns JSON with was_tripped + reset_at.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from parallax.router.circuit_breaker import get_breaker_state
from parallax.server.auth import require_auth

__all__ = ["router"]

router = APIRouter(prefix="/admin/circuit_breaker", tags=["admin"])


@router.post("/reset", dependencies=[Depends(require_auth)])
async def reset_circuit_breaker() -> dict:
    """Operator-only manual re-arm.

    Run ONLY after verifying Aphelion health — the breaker does NOT
    auto-recover (Q10 DECIDED, ralplan §10 line 552).

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
    state.reset()
    return {
        "ok": True,
        "was_tripped": was_tripped,
        "reset_at": datetime.now(UTC).isoformat(),
    }
