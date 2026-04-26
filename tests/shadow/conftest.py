"""Shared test helpers for ``tests/shadow/``.

Defining ``_record`` / ``_write_records`` here removes 3 copies of the same
helpers across ``test_discrepancy.py``, ``test_continuity_check.py``, and
``test_metrics_endpoint.py``. If the 9-field schema changes again, only this
file needs an update.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from parallax.shadow.discrepancy import SCHEMA_VERSION


def make_record(
    arbitration_outcome: str = "match",
    timestamp: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build a 9-field shadow decision record with sensible defaults."""
    base = {
        "arbitration_outcome": arbitration_outcome,
        "correlation_id": "cid-1",
        "crosswalk_status": "ok",
        "latency_ms": 1.0,
        "query_type": "recent_context",
        "schema_version": SCHEMA_VERSION,
        "selected_port": "QueryPort",
        "timestamp": timestamp or "2026-04-26T10:00:00.000000+00:00",
        "user_id": "alice",
    }
    base.update(overrides)
    return base


def write_records(log_dir: Path, records: list[dict[str, Any]], date: str = "2026-04-26") -> Path:
    """Append records to ``shadow-decisions-{date}.jsonl`` (deterministic JSONL form)."""
    path = log_dir / f"shadow-decisions-{date}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return path


@pytest.fixture()
def shadow_record() -> Any:
    """Fixture form: ``shadow_record(arbitration_outcome='diverge')``."""
    return make_record


@pytest.fixture()
def shadow_writer() -> Any:
    """Fixture form: ``shadow_writer(log_dir, [records...], date='2026-04-26')``."""
    return write_records
