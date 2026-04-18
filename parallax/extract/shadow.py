"""Dual-write shadow — safe wrapper around ``extract_and_ingest``.

Used on the a2a side behind a ``PARALLAX_DUAL_WRITE=1`` flag so the real
vault writer keeps running unchanged. Any exception from the provider or
the ingest bridge is swallowed and logged; the function always returns a
list (possibly empty), so the primary write path is blameless.

The emitted log record uses ``name='parallax_shadow_write'`` with a
structured ``extra`` dict — consumers should ship this to the same log
drain that monitors vault writes, then compare divergence offline.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from parallax.extract.ingest import extract_and_ingest
from parallax.extract.providers.base import Provider

__all__ = ["shadow_write"]

_default_logger = logging.getLogger("parallax_shadow_write")


def shadow_write(
    conn: sqlite3.Connection,
    text: str,
    *,
    provider: Provider,
    user_id: str,
    source_id: str | None = None,
    logger: logging.Logger | None = None,
) -> list[str]:
    """Run the shadow extract+ingest. Never raises. Always logs one record."""
    log = logger or _default_logger
    t0 = time.perf_counter()
    extra: dict[str, Any] = {
        "user_id": user_id,
        "source_id": source_id,
        "count": 0,
        "elapsed_ms": 0.0,
    }
    try:
        ids = extract_and_ingest(
            conn,
            text,
            provider=provider,
            user_id=user_id,
            source_id=source_id,
        )
    except Exception as exc:  # noqa: BLE001 — intentional broad catch on shadow path
        extra["elapsed_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        extra["error"] = repr(exc)
        # WARNING so silent shadow failures stay visible at default log level.
        log.warning("parallax_shadow_write", extra=extra)
        return []

    extra["count"] = len(ids)
    # claim_ids enables per-claim divergence comparison against the vault
    # write (count-only is too coarse for cutover decisions).
    extra["claim_ids"] = list(ids)
    extra["elapsed_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    log.info("parallax_shadow_write", extra=extra)
    return ids
