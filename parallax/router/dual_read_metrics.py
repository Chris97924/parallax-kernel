"""M3b — file-based dual-read metrics computation (US-006-M3-T2.3).

Mirrors :mod:`parallax.shadow.discrepancy` for the dual-read decision JSONL
stream. Pure module — no I/O at import. Thresholds live here so the metrics
endpoint and the DoD CLI both import from one source of truth.

Sources of data
---------------
The dual-read decision log is a per-day JSONL file under ``log_dir``:

  ``dual-read-decisions-YYYY-MM-DD.jsonl``

Each line is a single JSON object with at least:

  - ``outcome`` (or ``arbitration_outcome``) — one of ``match``,
    ``diverge``, ``primary_only``, ``aphelion_unreachable``, ``skipped``.
  - ``timestamp`` — ISO-8601 UTC microseconds.
  - ``data_quality_flag`` — optional, defaults to ``"normal"`` when missing.
  - ``crosswalk_status`` — optional, ``"miss"`` triggers crosswalk-miss
    counting.
  - ``circuit_breaker_tripped`` — optional bool.
  - ``write_error`` — optional bool.

Records with malformed JSON or unparseable timestamps are silently dropped
(they cannot be window-filtered safely).

Denominator semantics
---------------------
All rate denominators EXCLUDE ``aphelion_unreachable`` outcomes (mirrors
M2's ``shadow_only`` exclusion per ralplan §6 line 429). The exception is
:func:`aphelion_unreachable_rate`, where the unreachable count is itself
the numerator and the denominator is ALL outcomes.

Data-quality filtering
----------------------
The ``data_quality_filter`` argument defaults to
``["normal", "corpus_immature"]``. Records flagged ``cold_start`` are
excluded from production rate computation by default — pass an explicit
filter (e.g. including ``"cold_start"``) to opt back in for early-rollout
debug runs.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from parallax.shadow.discrepancy import parse_window

__all__ = [
    "DISCREPANCY_RATE_THRESHOLD_M3",
    "ARBITRATION_CONFLICT_RATE_THRESHOLD",
    "WRITE_ERROR_RATE_THRESHOLD",
    "APHELION_UNREACHABLE_THRESHOLD",
    "CROSSWALK_MISS_THRESHOLD",
    "CIRCUIT_OPEN_72H_MAX",
    "DEFAULT_DATA_QUALITY_FILTER",
    "discrepancy_rate",
    "arbitration_conflict_rate",
    "write_error_rate",
    "aphelion_unreachable_rate",
    "crosswalk_miss_rate",
    "circuit_open_count",
    "load_records",
]

# ---------------------------------------------------------------------------
# Threshold constants (single source of truth — imported by metrics.py + CLI)
# ---------------------------------------------------------------------------

DISCREPANCY_RATE_THRESHOLD_M3 = 0.001  # 0.1% — Q2 Option B
ARBITRATION_CONFLICT_RATE_THRESHOLD = 0.01  # 1%
WRITE_ERROR_RATE_THRESHOLD = 0.0002  # 0.02%
APHELION_UNREACHABLE_THRESHOLD = 0.005  # 0.5%
CROSSWALK_MISS_THRESHOLD = 0.05  # 5% — measured at +48h gate (Q11)
CIRCUIT_OPEN_72H_MAX = 3  # absolute count over the 72h window

# Records flagged with these data_quality_flag values count toward production
# rates by default. ``cold_start`` is excluded so early-rollout noise does not
# pollute the DoD numerics.
DEFAULT_DATA_QUALITY_FILTER: tuple[str, ...] = ("normal", "corpus_immature")

# ---------------------------------------------------------------------------
# Log directory + file pattern
# ---------------------------------------------------------------------------

_LOG_FILE_GLOB = "dual-read-decisions-*.jsonl"
_LOG_FILE_DATE_RE = re.compile(r"^dual-read-decisions-(\d{4}-\d{2}-\d{2})\.jsonl$")
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "parallax" / "logs"


def _resolve_log_dir(log_dir: Path | str | None) -> Path:
    """Resolve to an absolute path. ``.resolve()`` normalises any symlinks."""
    if log_dir is not None:
        return Path(log_dir).resolve()
    env = os.environ.get("DUAL_READ_LOG_DIR")
    if env:
        return Path(env).resolve()
    return _DEFAULT_LOG_DIR.resolve()


def _parse_timestamp(raw: str) -> _dt.datetime | None:
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed


def _file_date(path: Path) -> _dt.date | None:
    match = _LOG_FILE_DATE_RE.match(path.name)
    if match is None:
        return None
    try:
        return _dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _normalize_outcome(record: dict[str, Any]) -> str | None:
    """Return the outcome string. Tolerates both field names (compat with shadow JSONL)."""
    val = record.get("outcome")
    if isinstance(val, str) and val:
        return val
    val = record.get("arbitration_outcome")
    if isinstance(val, str) and val:
        return val
    return None


def load_records(
    *,
    log_dir: Path | str | None = None,
    since: _dt.timedelta | None = None,
    now: _dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Load and parse all dual-read decision records under ``log_dir``.

    Records older than ``now - since`` (when ``since`` is given) are dropped.
    Records with unparseable JSON or unparseable timestamps are dropped
    silently — they cannot be window-filtered safely. Returns records sorted
    by timestamp ascending.
    """
    resolved_dir = _resolve_log_dir(log_dir)
    if not resolved_dir.is_dir():
        return []

    cutoff: _dt.datetime | None = None
    if since is not None:
        anchor = now if now is not None else _dt.datetime.now(_dt.UTC)
        cutoff = anchor - since
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=_dt.UTC)

    records: list[tuple[_dt.datetime, dict[str, Any]]] = []
    for path in sorted(resolved_dir.glob(_LOG_FILE_GLOB)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        file_date = _file_date(path)
        # Conservative file-level prefilter: skip files whose UTC date is
        # entirely before the cutoff date.
        if cutoff is not None and file_date is not None and file_date < cutoff.date():
            continue
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            ts_raw = parsed.get("timestamp")
            ts = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
            if ts is None:
                continue
            if cutoff is not None and ts < cutoff:
                continue
            records.append((ts, parsed))

    records.sort(key=lambda triple: triple[0])
    return [r for _, r in records]


def _filter_by_quality(
    records: Iterable[dict[str, Any]],
    data_quality_filter: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Keep only records whose ``data_quality_flag`` is in ``data_quality_filter``.

    Records missing the flag are treated as ``"normal"`` (default).
    """
    allowed = (
        set(DEFAULT_DATA_QUALITY_FILTER)
        if data_quality_filter is None
        else set(data_quality_filter)
    )
    out: list[dict[str, Any]] = []
    for r in records:
        flag = r.get("data_quality_flag")
        flag = flag if isinstance(flag, str) and flag else "normal"
        if flag in allowed:
            out.append(r)
    return out


def _excluding_unreachable(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out ``aphelion_unreachable`` outcomes (denominator exclusion)."""
    return [r for r in records if _normalize_outcome(r) != "aphelion_unreachable"]


def _load_filtered(
    *,
    window: str,
    log_dir: Path | str | None,
    now: _dt.datetime | None,
    data_quality_filter: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Common path: parse window, load, filter by quality."""
    delta = parse_window(window)
    raw = load_records(log_dir=log_dir, since=delta, now=now)
    return _filter_by_quality(raw, data_quality_filter)


# ---------------------------------------------------------------------------
# Public rate functions
# ---------------------------------------------------------------------------


def discrepancy_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of in-window dual-read outcomes that are ``"diverge"``.

    Denominator excludes ``aphelion_unreachable``. Empty window → 0.0.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    denom_records = _excluding_unreachable(records)
    if not denom_records:
        return 0.0
    diverge = sum(1 for r in denom_records if _normalize_outcome(r) == "diverge")
    return diverge / len(denom_records)


def arbitration_conflict_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of dual-read outcomes that triggered an arbitration conflict.

    A record is a conflict when:
      - ``winning_source`` is ``"tie"`` or ``"fallback"`` (live arbitration
        verdict requires manual review), OR
      - ``conflict_event_id`` is a non-empty string (writer fired).

    Denominator excludes ``aphelion_unreachable``. Note that a plain
    ``"diverge"`` outcome alone is a discrepancy, NOT a conflict — the two
    metrics are intentionally distinct (Q1 Option A rule table treats
    most diverge cases as a clean parallax/aphelion win, not a conflict).
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    denom_records = _excluding_unreachable(records)
    if not denom_records:
        return 0.0
    conflicts = 0
    for r in denom_records:
        ws = r.get("winning_source")
        if isinstance(ws, str) and ws in ("tie", "fallback"):
            conflicts += 1
            continue
        eid = r.get("conflict_event_id")
        if isinstance(eid, str) and eid:
            conflicts += 1
    return conflicts / len(denom_records)


def write_error_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of dual-read outcomes that report a write error.

    Denominator excludes ``aphelion_unreachable``.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    denom_records = _excluding_unreachable(records)
    if not denom_records:
        return 0.0
    errors = sum(1 for r in denom_records if r.get("write_error") is True)
    return errors / len(denom_records)


def aphelion_unreachable_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of in-window dual-read outcomes that are ``"aphelion_unreachable"``.

    Denominator: ALL outcomes (not excluded). Empty window → 0.0.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    if not records:
        return 0.0
    unreachable = sum(1 for r in records if _normalize_outcome(r) == "aphelion_unreachable")
    return unreachable / len(records)


def crosswalk_miss_rate(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> float:
    """Fraction of in-window dual-read records with ``crosswalk_status == "miss"``.

    Denominator excludes ``aphelion_unreachable``. Per Q11, the operational
    measurement window is +48h from rollout.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    denom_records = _excluding_unreachable(records)
    if not denom_records:
        return 0.0
    misses = sum(1 for r in denom_records if r.get("crosswalk_status") == "miss")
    return misses / len(denom_records)


def circuit_open_count(
    window: str,
    *,
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
    data_quality_filter: Sequence[str] | None = None,
) -> int:
    """Count of in-window dual-read records flagged with circuit-breaker open.

    Returns an integer (not a rate). The DoD threshold is the absolute cap
    :data:`CIRCUIT_OPEN_72H_MAX` over a 72h window.
    """
    records = _load_filtered(
        window=window, log_dir=log_dir, now=now, data_quality_filter=data_quality_filter
    )
    return sum(1 for r in records if r.get("circuit_breaker_tripped") is True)
