"""Structured JSON logging for Parallax.

Each record is a single-line JSON object with fixed keys (``ts``, ``level``,
``logger``, ``msg``) plus any ``extra=...`` kwargs flattened onto the top
level. Emits to stderr by default so it never collides with stdout payloads
in CLI-driven callers.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any

__all__ = ["get_logger", "JSONFormatter"]

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc).isoformat(),
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
    """Return a JSON-formatted logger. Idempotent per-name."""
    logger = logging.getLogger(name)
    if not any(getattr(h, "_parallax_json", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JSONFormatter())
        handler._parallax_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
