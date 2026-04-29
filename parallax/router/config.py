"""MEMORY_ROUTER feature flag (Lane D-1).

MEMORY_ROUTER is a module-level Final[bool] captured at import time (production path).
is_router_enabled() re-reads os.environ at call time (dynamic path for tests).

Truthy: only the literal string 'true' (case-insensitive). Everything else -> False.
"""

from __future__ import annotations

import os
from typing import Final

__all__ = ["MEMORY_ROUTER", "is_router_enabled", "is_dual_read_enabled"]


def is_router_enabled() -> bool:
    """Re-read MEMORY_ROUTER env var at call time.

    Accepts only 'true' (case-insensitive) as truthy. All other values -> False.
    """
    return os.getenv("MEMORY_ROUTER", "false").strip().lower() == "true"


def is_dual_read_enabled() -> bool:
    """Re-read DUAL_READ env var at call time.

    Accepts only 'true' (case-insensitive) as truthy. All other values -> False.

    Function form only — no Final[bool] constant.  Per Q8 decision (ralplan §10):
    T1.4 middleware will snapshot this per-request via FastAPI Depends(), not at
    import time.  Keeping it as a function avoids the frozen-at-import footgun.
    """
    return os.getenv("DUAL_READ", "false").strip().lower() == "true"


MEMORY_ROUTER: Final[bool] = is_router_enabled()
