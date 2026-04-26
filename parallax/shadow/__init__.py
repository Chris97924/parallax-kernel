"""Lane C v0.2.0-beta — Shadow observability surface.

Public API:
- :mod:`parallax.shadow.discrepancy` — discrepancy_rate, checksum_consistency,
  compute_checksum_chain, parse_window, load_records.

The shadow router itself lives at :mod:`parallax.router.shadow`. This package
contains the WS-3 read-side: parsers, aggregators, and metrics for the daily
JSONL decision-log files that the router writes.
"""

from __future__ import annotations

from parallax.shadow.discrepancy import (
    CHECKSUM_CONSISTENCY_THRESHOLD,
    DISCREPANCY_RATE_THRESHOLD,
    SCHEMA_VERSION,
    checksum_consistency,
    compute_checksum_chain,
    discrepancy_rate,
    load_records,
    parse_window,
)

__all__ = [
    "CHECKSUM_CONSISTENCY_THRESHOLD",
    "DISCREPANCY_RATE_THRESHOLD",
    "SCHEMA_VERSION",
    "checksum_consistency",
    "compute_checksum_chain",
    "discrepancy_rate",
    "load_records",
    "parse_window",
]
