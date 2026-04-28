"""M3-T1.4 — Per-request DUAL_READ flag-snapshot middleware (US-011).

Snapshots ``is_dual_read_enabled()`` exactly once at request entry and
stores the result in ``request.state.dual_read``.  Downstream route handlers
MUST read the flag exclusively via ``request.state.dual_read`` — they must
NEVER call ``os.environ`` or ``is_dual_read_enabled()`` mid-request, because
an operator can flip the env flag for a rollback at any moment.  The snapshot
freezes the flag for the duration of this request so in-flight work is
consistent.

The circuit breaker is consulted at the same snapshot moment: if it is
tripped the effective flag is forced to ``False`` regardless of the env var
(fail-closed, per Q8 DECIDED, ralplan §10 2026-04-27).

Critical bug-guard
------------------
The inflight gauge increment/decrement is wrapped in ``try/finally`` so the
count is ALWAYS decremented — even on auth errors, 422 validation failures,
410 Gone, or bare RuntimeErrors.  Without ``try/finally`` the gauge would
leak on exception paths and the 15-minute drain promise would break silently.
This is the exact bug T1.4 is designed to prevent.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from parallax.router.circuit_breaker import get_breaker_state
from parallax.router.config import is_dual_read_enabled
from parallax.router.inflight import inflight_gauge

__all__ = ["DualReadSnapshotMiddleware", "install_middleware"]


class DualReadSnapshotMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that:

    1. Snapshots ``DUAL_READ`` env flag + circuit-breaker state once at
       request entry into ``request.state.dual_read``.
    2. Wraps the rest of the request in the inflight gauge with a
       ``try/finally`` guard that always decrements on exit.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:  # type: ignore[override]
        # 1. Snapshot the effective flag at request boundary.
        env_says_true = is_dual_read_enabled()
        breaker_tripped = get_breaker_state().is_tripped()
        request.state.dual_read = env_says_true and not breaker_tripped

        # 2. CRITICAL try/finally — inflight gauge MUST decrement even when
        #    call_next raises (auth errors, validation errors, bare exceptions).
        inflight_gauge.inc()
        try:
            response = await call_next(request)
            return response
        finally:
            inflight_gauge.dec()


def install_middleware(app: FastAPI) -> None:
    """Register :class:`DualReadSnapshotMiddleware` on *app*.

    Convenience wrapper so ``create_app()`` only needs one import.
    Do NOT call this at module import time — call it inside ``create_app()``
    so the middleware is scoped to the specific app instance (test isolation).
    """
    app.add_middleware(DualReadSnapshotMiddleware)
