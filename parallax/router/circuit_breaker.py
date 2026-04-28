"""M3-T1.4 — BreakerState singleton stub (US-011).

STUB — full rolling-window logic ships in M3-T1.5.  ``is_tripped()`` always
returns ``False`` here so the T1.4 middleware contract can settle before
T1.5 lands.

Design notes (§10 Q10 ralplan 2026-04-27, lines 547-555):
- Process-local state only.  DOES NOT mutate ``os.environ``.
- Singleton: a single ``BreakerState`` instance lives for the lifetime of the
  Python process.  Tests that need a clean slate should call ``reset()``
  or monkey-patch ``get_breaker_state`` to return a fresh instance.
- ``_thread_lock`` is reserved for T1.5's rolling-window mutation path;
  T1.4 read paths are lock-free (reading a bool is atomic in CPython).
- ``record_unreachable_observation`` is a NO-OP in T1.4; T1.5 will fill in
  the rolling-window increment logic.
- ``reset()`` is exercisable from the future ``/admin/circuit_breaker/reset``
  endpoint (T1.5+).
"""

from __future__ import annotations

import threading
from datetime import datetime

__all__ = ["BreakerState", "get_breaker_state"]

# ---------------------------------------------------------------------------
# BreakerState
# ---------------------------------------------------------------------------


class BreakerState:
    """Process-local circuit-breaker state for the dual-read path.

    T1.4 stub: ``is_tripped()`` always returns ``False``.  T1.5 will add
    rolling-window observation + automatic trip logic.
    """

    def __init__(self) -> None:
        self.tripped: bool = False
        self.tripped_at: datetime | None = None
        # Reserved for T1.5 rolling-window mutations.
        self._thread_lock: threading.Lock = threading.Lock()

    def is_tripped(self) -> bool:
        """Return whether the breaker is currently tripped.

        T1.4 stub: always returns ``False``.
        """
        return self.tripped

    def record_unreachable_observation(self, *, observed_unreachable: bool) -> None:
        """Record one unreachable observation from the dual-read path.

        T1.4 stub: NO-OP.  T1.5 will implement the rolling-window counter
        and automatic trip logic here.
        """
        # NO-OP in T1.4

    def reset(self) -> None:
        """Reset the breaker to its default (non-tripped) state.

        Called by the future ``/admin/circuit_breaker/reset`` endpoint and by
        tests that need a clean slate between runs.
        """
        with self._thread_lock:
            self.tripped = False
            self.tripped_at = None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_BREAKER_STATE_SINGLETON: BreakerState | None = None
_SINGLETON_LOCK: threading.Lock = threading.Lock()


def get_breaker_state() -> BreakerState:
    """Return the process-local BreakerState singleton.

    Lazy initialisation with double-checked locking so the singleton is
    safe to call from any thread.  Singleton-by-design: process-local
    circuit-breaker state must be shared across all request handlers
    (a new instance per request would defeat the purpose of a breaker).
    """
    global _BREAKER_STATE_SINGLETON  # noqa: PLW0603
    if _BREAKER_STATE_SINGLETON is None:
        with _SINGLETON_LOCK:
            if _BREAKER_STATE_SINGLETON is None:
                _BREAKER_STATE_SINGLETON = BreakerState()
    return _BREAKER_STATE_SINGLETON
