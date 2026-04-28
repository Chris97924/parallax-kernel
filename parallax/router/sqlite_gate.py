"""M3-T1.2 — Thread-safe SQLite gate for dual-read cross-thread access (US-011).

All dual-read sqlite access routes through ``SQLiteGate``.  Existing M1/M2
callers stay on raw ``sqlite3.Connection``; only the new dual-read code path
uses this abstraction.

Q4 (ralplan §10 lines 587-616) must-do implementation notes:
  - Single ``threading.Lock`` serialises all cross-thread access on a shared conn.
  - Cursor lifecycle MUST end inside the lock: ``fetch_all`` calls ``fetchall()``
    before releasing, so no cursor ever crosses the lock boundary (corruption risk).
  - WAL pragmas applied on first construction per connection; idempotent across
    subsequent ``SQLiteGate`` instances wrapping the same ``sqlite3.Connection``.
    ``_pragma_applied`` is a class-level set keyed on ``id(connection)`` — this
    assumes the caller does NOT close and re-open the same memory address for a
    different logical db (safe for the current ZenBook single-process deployment).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Literal

import prometheus_client

from parallax.obs.log import get_logger

__all__ = ["SQLiteGate", "SQLiteGateMetrics"]

_log = get_logger("parallax.router.sqlite_gate")

# ---------------------------------------------------------------------------
# Prometheus helpers (re-import safe)
# ---------------------------------------------------------------------------

_LOCK_WAIT_BUCKETS = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]

_COMPONENT_LABEL_VALUES = frozenset({"ingest", "m2_shadow", "regular_query", "m3_dual_read"})
_OP_LABEL_VALUES = frozenset({"read", "write"})


def _get_or_create_histogram(
    name: str, doc: str, labelnames: list[str], buckets: list[float]
) -> prometheus_client.Histogram:
    try:
        return prometheus_client.Histogram(name, doc, labelnames, buckets=buckets)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name + "_bucket"]  # type: ignore[return-value]


def _get_or_create_gauge(name: str, doc: str, labelnames: list[str]) -> prometheus_client.Gauge:
    try:
        return prometheus_client.Gauge(name, doc, labelnames)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


def _get_or_create_counter(name: str, doc: str, labelnames: list[str]) -> prometheus_client.Counter:
    try:
        return prometheus_client.Counter(name, doc, labelnames)
    except ValueError:
        return prometheus_client.REGISTRY._names_to_collectors[name + "_total"]  # type: ignore[return-value]


_lock_wait_hist = _get_or_create_histogram(
    "parallax_sqlite_lock_wait_seconds",
    "Time spent waiting to acquire the SQLiteGate lock, by component and op.",
    ["component", "op"],
    _LOCK_WAIT_BUCKETS,
)

_lock_hold_hist = _get_or_create_histogram(
    "parallax_sqlite_lock_hold_seconds",
    "Time spent holding the SQLiteGate lock, by component and op.",
    ["component", "op"],
    _LOCK_WAIT_BUCKETS,
)

_queue_depth_gauge = _get_or_create_gauge(
    "parallax_sqlite_lock_queue_depth",
    "Number of threads currently waiting for the SQLiteGate lock (process-wide).",
    [],
)

_wal_size_gauge = _get_or_create_gauge(
    "parallax_sqlite_wal_size_bytes",
    "Approximate size of the SQLite WAL file in bytes (lazily sampled).",
    [],
)

_errors_counter = _get_or_create_counter(
    "parallax_sqlite_errors_total",
    "Count of sqlite errors by error class, component, and op.",
    ["code", "component", "op"],
)


class SQLiteGateMetrics:
    """Namespace exposing the module-level Prometheus collectors for inspection."""

    lock_wait = _lock_wait_hist
    lock_hold = _lock_hold_hist
    queue_depth = _queue_depth_gauge
    wal_size = _wal_size_gauge
    errors = _errors_counter


# ---------------------------------------------------------------------------
# Cancellable (used by start_background_checkpoint)
# ---------------------------------------------------------------------------


class _Cancellable:
    """Object returned by ``start_background_checkpoint``; caller calls ``.stop()``."""

    def __init__(self, event: threading.Event) -> None:
        self._stop_event = event

    def stop(self) -> None:
        """Signal the background checkpoint thread to exit."""
        self._stop_event.set()


# ---------------------------------------------------------------------------
# SQLiteGate
# ---------------------------------------------------------------------------


class SQLiteGate:
    """Thread-safe wrapper around a ``sqlite3.Connection`` for dual-read access.

    Usage::

        gate = SQLiteGate(conn, component="m3_dual_read")
        rows = gate.fetch_all("SELECT ...", (param,))
        row  = gate.fetch_one("SELECT ... LIMIT 1", (param,))
        gate.execute("UPDATE ...", (param,))
        gate.executemany("INSERT ...", batch)

    WAL pragma assumption:
        ``_pragma_applied`` is keyed on ``id(connection)`` — assumes the caller
        does NOT close and re-open a new connection at the same memory address for
        a different logical database within the same process lifetime.

    component label values: ``ingest`` | ``m2_shadow`` | ``regular_query`` | ``m3_dual_read``
    """

    _pragma_applied: set[int] = set()
    _pragma_lock = threading.Lock()  # guards _pragma_applied across threads

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        component: str = "m3_dual_read",
    ) -> None:
        if component not in _COMPONENT_LABEL_VALUES:
            raise ValueError(f"component={component!r} not in {sorted(_COMPONENT_LABEL_VALUES)}")
        self._conn = connection
        self._component = component
        self._lock = threading.Lock()
        self._apply_wal_pragmas_once()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_wal_pragmas_once(self) -> None:
        """Apply WAL pragmas on the first SQLiteGate constructed for this conn.

        Uses a class-level set keyed on ``id(connection)`` so that subsequent
        ``SQLiteGate`` instances wrapping the same connection skip re-application.
        The pragma set operation is itself serialised by ``_pragma_lock``.
        """
        conn_id = id(self._conn)
        with SQLiteGate._pragma_lock:
            if conn_id in SQLiteGate._pragma_applied:
                return
            # Apply inside the class-level pragma lock (not the instance lock, which
            # doesn't exist yet when this is called from __init__).
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA wal_autocheckpoint=0")
            cur.close()
            SQLiteGate._pragma_applied.add(conn_id)

    def _db_file(self) -> str | None:
        """Return the database file path from ``PRAGMA database_list``, or None."""
        try:
            cur = self._conn.cursor()
            cur.execute("PRAGMA database_list")
            rows = cur.fetchall()
            cur.close()
            for row in rows:
                # (seq, name, file) — we want the 'main' database file
                if row[1] == "main" and row[2]:
                    return row[2]
        except Exception:  # noqa: BLE001
            pass
        return None

    def _sample_wal_size(self) -> None:
        """Update the WAL size gauge lazily (cheap os.path.getsize)."""
        db_file = self._db_file()
        if db_file:
            wal_path = db_file + "-wal"
            try:
                size = os.path.getsize(wal_path)
                _wal_size_gauge.set(size)
            except OSError:
                pass  # WAL file may not exist for in-memory DBs

    def _execute_op(
        self,
        op: Literal["read", "write"],
        sql: str,
        params: tuple[Any, ...] | list[tuple[Any, ...]] | None,
        *,
        many: bool = False,
        one: bool = False,
    ) -> list[Any]:
        """Core lock-guarded execution helper.

        Returns:
          - fetch_all path: list of rows
          - fetch_one path: list with at most one row
          - write path: empty list
        """
        # Signal that we are waiting for the lock (process-wide queue depth).
        _queue_depth_gauge.inc()
        wait_start = time.perf_counter()
        try:
            with self._lock:
                wait_elapsed = time.perf_counter() - wait_start
                _queue_depth_gauge.dec()
                _lock_wait_hist.labels(component=self._component, op=op).observe(wait_elapsed)

                hold_start = time.perf_counter()
                try:
                    cur = self._conn.cursor()
                    if many:
                        cur.executemany(sql, params)  # type: ignore[arg-type]
                        result: list[Any] = []
                    elif one:
                        cur.execute(sql, params or ())
                        row = cur.fetchone()
                        result = [row] if row is not None else []
                    elif op == "read":
                        cur.execute(sql, params or ())
                        # Materialise BEFORE releasing lock — Q4 must-do patch.
                        result = cur.fetchall()
                    else:
                        cur.execute(sql, params or ())
                        result = []
                    cur.close()
                except sqlite3.Error as exc:
                    _errors_counter.labels(
                        code=type(exc).__name__,
                        component=self._component,
                        op=op,
                    ).inc()
                    raise
                finally:
                    hold_elapsed = time.perf_counter() - hold_start
                    _lock_hold_hist.labels(component=self._component, op=op).observe(hold_elapsed)
        except sqlite3.Error:
            raise
        except Exception:
            # Ensure queue depth is decremented even on unexpected errors that
            # occur before the lock is acquired.
            raise
        else:
            if op == "read":
                self._sample_wal_size()
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_all(self, sql: str, params: tuple[Any, ...] | None = None) -> list[Any]:
        """Execute SELECT; return all rows as a list (materialised inside the lock)."""
        return self._execute_op("read", sql, params)

    def fetch_one(self, sql: str, params: tuple[Any, ...] | None = None) -> Any | None:
        """Execute SELECT LIMIT 1; return the first row or None (materialised inside the lock)."""
        rows = self._execute_op("read", sql, params, one=True)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        """Execute a single write statement inside the lock."""
        self._execute_op("write", sql, params)

    def executemany(self, sql: str, batch: list[tuple[Any, ...]]) -> None:
        """Execute a batched write statement inside the lock."""
        self._execute_op("write", sql, batch, many=True)

    def start_background_checkpoint(self, *, interval_seconds: float = 300.0) -> _Cancellable:
        """Spawn a daemon thread running ``PRAGMA wal_checkpoint(PASSIVE)`` every N seconds.

        The returned ``Cancellable`` has a ``.stop()`` method.  The checkpoint
        thread is a daemon so it does not prevent process exit if ``stop()`` is
        not called.

        Auto-checkpoint is disabled on construction (``wal_autocheckpoint=0``).
        Callers that need periodic checkpointing should call this method explicitly.
        """
        stop_event = threading.Event()

        def _run() -> None:
            while not stop_event.wait(timeout=interval_seconds):
                try:
                    with self._lock:
                        cur = self._conn.cursor()
                        cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
                        cur.close()
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "wal_checkpoint_failed",
                        extra={"event": "wal_checkpoint_failed", "error": str(exc)},
                    )

        t = threading.Thread(target=_run, daemon=True, name="sqlite-gate-checkpoint")
        t.start()
        return _Cancellable(stop_event)
