"""Parallax telemetry — single-file stdlib observability.

Structured JSON events under ``parallax.*``: dedup_hit / state_changed /
ingest_error / orphan_rejected. Thread-safe counters (ingested_total,
dedup_hits_total, errors_total) + latency p50/p95/p99 (nearest-rank, bounded
ring buffer). ``health(db_path)`` returns db path, table counts, WAL mode,
last error. No Prometheus exporter by design (YAGNI).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import pathlib
import sqlite3
import sys
import threading
from typing import Any

__all__ = [
    "get_logger",
    "emit_dedup_hit",
    "emit_state_changed",
    "emit_ingest_error",
    "emit_orphan_rejected",
    "inc",
    "observe_latency_ms",
    "snapshot",
    "reset",
    "health",
]

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a JSON-formatted logger. Idempotent per name."""
    logger = logging.getLogger(name)
    if not any(getattr(h, "_parallax_telemetry", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JSONFormatter())
        handler._parallax_telemetry = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


# ----- Metrics state --------------------------------------------------------

_METRICS_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {
    "ingested_total": 0,
    "dedup_hits_total": 0,
    "errors_total": 0,
}
_LATENCY_CAP = 1024
_LATENCIES: list[float] = []
_LATENCY_IDX = 0

_ERROR_LOCK = threading.Lock()
_LAST_ERROR: str | None = None


def inc(name: str, n: int = 1) -> None:
    """Increment a named counter. Unknown names are created on first use."""
    with _METRICS_LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + n


def observe_latency_ms(value: float) -> None:
    """Record a latency sample (milliseconds) into the bounded ring buffer."""
    global _LATENCY_IDX
    with _METRICS_LOCK:
        if len(_LATENCIES) < _LATENCY_CAP:
            _LATENCIES.append(float(value))
        else:
            _LATENCIES[_LATENCY_IDX] = float(value)
            _LATENCY_IDX = (_LATENCY_IDX + 1) % _LATENCY_CAP


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(pct / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def snapshot() -> dict[str, Any]:
    """Return a point-in-time view of all counters + latency percentiles."""
    with _METRICS_LOCK:
        samples = sorted(_LATENCIES)
        out: dict[str, Any] = dict(_COUNTERS)
    with _ERROR_LOCK:
        last_error = _LAST_ERROR
    out["latency_p50_ms"] = _percentile(samples, 50)
    out["latency_p95_ms"] = _percentile(samples, 95)
    out["latency_p99_ms"] = _percentile(samples, 99)
    out["last_error"] = last_error
    return out


def reset() -> None:
    """Zero every counter, clear the latency buffer and last_error."""
    global _LATENCY_IDX, _LAST_ERROR
    with _METRICS_LOCK:
        for k in list(_COUNTERS.keys()):
            _COUNTERS[k] = 0
        _LATENCIES.clear()
        _LATENCY_IDX = 0
    with _ERROR_LOCK:
        _LAST_ERROR = None


# ----- Event emitters -------------------------------------------------------


def _emit(logger: logging.Logger, level: int, event: str, **extra: Any) -> None:
    payload = {"event": event, **extra}
    logger.log(level, event, extra=payload)


def emit_dedup_hit(logger: logging.Logger, **extra: Any) -> None:
    _emit(logger, logging.INFO, "dedup_hit", **extra)


def emit_state_changed(logger: logging.Logger, **extra: Any) -> None:
    _emit(logger, logging.INFO, "state_changed", **extra)


def emit_orphan_rejected(logger: logging.Logger, **extra: Any) -> None:
    _emit(logger, logging.INFO, "orphan_rejected", **extra)


def emit_ingest_error(logger: logging.Logger, **extra: Any) -> None:
    global _LAST_ERROR
    msg = str(extra.get("error", ""))
    with _ERROR_LOCK:
        _LAST_ERROR = f"{_dt.datetime.now(_dt.UTC).isoformat()} {msg}".strip()
    inc("errors_total")
    _emit(logger, logging.ERROR, "ingest_error", **extra)


# ----- Health ---------------------------------------------------------------

_HEALTH_TABLES = ("sources", "memories", "claims", "decisions", "events", "index_state")


def health(db_path: pathlib.Path | str) -> dict[str, Any]:
    """Return DB path, table counts, WAL mode, and last recorded error."""
    resolved = str(pathlib.Path(db_path).resolve())
    counts: dict[str, int] = {}
    journal_mode = ""
    conn = sqlite3.connect(resolved)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("PRAGMA journal_mode").fetchone()
        journal_mode = str(row[0]).lower() if row else ""
        for t in _HEALTH_TABLES:
            if not t.isidentifier():
                counts[t] = 0
                continue
            try:
                r = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
                counts[t] = int(r[0])
            except sqlite3.OperationalError:
                counts[t] = 0
    finally:
        conn.close()
    with _ERROR_LOCK:
        last_error = _LAST_ERROR
    return {
        "db_path": resolved,
        "table_counts": counts,
        "journal_mode": journal_mode,
        "last_error": last_error,
    }
