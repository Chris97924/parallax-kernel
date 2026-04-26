"""WS-3 — discrepancy detector + checksum-chain over shadow JSONL decision logs.

Public API (matches the runbook contract in ``docs/lane-c/m2-rollout-runbook.md``):

- ``discrepancy_rate(window='1h')`` — fraction of records in the most recent
  window whose ``arbitration_outcome == "diverge"``. DoD: ``≤ 0.003``.
- ``checksum_consistency(window='1h')`` — fraction of records in the most
  recent window that are well-formed (parseable, exactly the 9 canonical
  fields, ``schema_version`` matches, deterministic round-trip stable).
  DoD: ``≥ 0.999``.
- ``compute_checksum_chain(records)`` — rolling SHA-256 over deterministic
  JSONL forms; used by the continuity-check CLI to detect insertion / deletion.

All functions accept ``log_dir`` and ``now`` parameters for testability; in
production they default to ``$SHADOW_LOG_DIR`` / wall-clock UTC respectively.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from parallax.router.shadow import SCHEMA_VERSION, ShadowDecisionLog

__all__ = [
    "CHECKSUM_CONSISTENCY_THRESHOLD",
    "DISCREPANCY_RATE_THRESHOLD",
    "SCHEMA_VERSION",
    "LoadResult",
    "checksum_consistency",
    "compute_checksum_chain",
    "discrepancy_rate",
    "is_record_consistent",
    "load_records",
    "parse_window",
]

# Pinned to runbook DoD numerics.
DISCREPANCY_RATE_THRESHOLD = 0.003
CHECKSUM_CONSISTENCY_THRESHOLD = 0.999

# Derived from the canonical ShadowDecisionLog dataclass so a future field add
# in parallax.router.shadow propagates here automatically. The drift guard test
# in tests/shadow/test_discrepancy.py asserts the count and member set.
_CANONICAL_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(ShadowDecisionLog))

# Default mirrors parallax.config._DEFAULT_SHADOW_LOG_DIR. Computed here from
# __file__ instead of imported to avoid a parallax.config dependency in this
# leaf module — drift risk is documented in tests/test_config.py.
_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "parallax" / "logs"
_LOG_FILE_GLOB = "shadow-decisions-*.jsonl"
_LOG_FILE_DATE_RE = re.compile(r"^shadow-decisions-(\d{4}-\d{2}-\d{2})\.jsonl$")
_WINDOW_RE = re.compile(r"^(?P<n>\d+)(?P<unit>[smhd])$")
# Forward-compat: schema_version "1.x" is accepted; "2.0" forces the consistency
# check to fail so an unannounced major bump surfaces immediately.
_SCHEMA_VERSION_PREFIX = SCHEMA_VERSION.split(".", 1)[0] + "."


# ---------------------------------------------------------------------------
# parse_window
# ---------------------------------------------------------------------------


def parse_window(spec: str) -> _dt.timedelta:
    """Parse strings like ``'1h'`` / ``'30m'`` / ``'72h'`` / ``'3d'`` / ``'90s'``.

    Bare integers and zero/negative values are rejected — the runbook contract
    is explicit about units, and a 0 window has no meaningful interpretation
    for either of the WS-3 metrics.
    """
    if not isinstance(spec, str) or not spec:
        raise ValueError(f"invalid window: {spec!r}")
    match = _WINDOW_RE.match(spec)
    if match is None:
        raise ValueError(f"invalid window: {spec!r} (expected e.g. '1h', '30m', '72h')")
    n = int(match["n"])
    if n <= 0:
        raise ValueError(f"window must be positive: {spec!r}")
    unit = match["unit"]
    if unit == "s":
        return _dt.timedelta(seconds=n)
    if unit == "m":
        return _dt.timedelta(minutes=n)
    if unit == "h":
        return _dt.timedelta(hours=n)
    if unit == "d":
        return _dt.timedelta(days=n)
    # Unreachable: regex constrains the unit set.
    raise ValueError(f"unsupported window unit: {unit!r}")


# ---------------------------------------------------------------------------
# load_records
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LoadResult:
    """Parsed shadow records plus a count of unparseable lines.

    ``records`` are the successfully-parsed dicts (sorted by ``timestamp``).
    ``raw_lines`` zip-aligns with ``records`` so consistency checks can verify
    deterministic round-trip on the on-disk byte form.
    ``malformed`` is the count of JSON-parse failures across all input files
    that fall within the requested window. Malformed lines have no timestamp,
    so window-filtering uses the daily file's UTC date — a malformed line is
    in-window iff its file's date is on or after the window's cutoff date.
    """

    records: list[dict[str, Any]]
    raw_lines: list[str]
    malformed: int


def _resolve_log_dir(log_dir: Path | str | None) -> Path:
    """Resolve to an absolute path. ``.resolve()`` normalises any symlinks so the
    glob below cannot follow a symlink named ``shadow-decisions-evil.jsonl`` to
    an arbitrary host file (defense-in-depth — the writer is trusted, but the
    operator-controlled SHADOW_LOG_DIR / --log-dir flag is not)."""
    if log_dir is not None:
        return Path(log_dir).resolve()
    env = os.environ.get("SHADOW_LOG_DIR")
    if env:
        return Path(env).resolve()
    return _DEFAULT_LOG_DIR.resolve()


def _parse_timestamp(raw: str) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp; force UTC on naive results.

    Naive timestamps would otherwise raise ``TypeError`` on the
    ``record_ts >= cutoff`` comparison in ``load_records`` (cutoff is always
    UTC-aware). The shadow router writes microsecond UTC offsets, but a
    third-party / replayed record could land naive — bricking the entire
    /metrics endpoint and CLI. UTC is the documented default for the daily
    file rotation, so naive → UTC is the correct interpretation.
    """
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed


def _file_date(path: Path) -> _dt.date | None:
    """Extract the UTC date encoded in a ``shadow-decisions-YYYY-MM-DD.jsonl`` filename."""
    match = _LOG_FILE_DATE_RE.match(path.name)
    if match is None:
        return None
    try:
        return _dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def load_records(
    *,
    log_dir: Path | str | None = None,
    since: _dt.timedelta | None = None,
    now: _dt.datetime | None = None,
) -> LoadResult:
    """Load and parse all ``shadow-decisions-*.jsonl`` records from ``log_dir``.

    When ``since`` is supplied, records older than ``now - since`` are dropped.
    Records with unparseable timestamps are dropped (and counted as malformed)
    even when ``since`` is None — a record without a timestamp violates the
    9-field schema contract and should not pollute downstream metrics.

    Malformed counts are window-filtered by the source file's UTC date: a
    malformed line in ``shadow-decisions-2026-04-26.jsonl`` is included in
    a 1h-window check at 12:00 on 2026-04-26 because the file's date matches
    the cutoff date. This is a conservative approximation — we cannot pin a
    malformed line to a specific moment within the day.
    """
    resolved_dir = _resolve_log_dir(log_dir)
    if not resolved_dir.is_dir():
        return LoadResult(records=[], raw_lines=[], malformed=0)

    cutoff: _dt.datetime | None = None
    if since is not None:
        anchor = now if now is not None else _dt.datetime.now(_dt.UTC)
        cutoff = anchor - since
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=_dt.UTC)

    records: list[tuple[_dt.datetime, dict[str, Any], str]] = []
    malformed = 0

    for path in sorted(resolved_dir.glob(_LOG_FILE_GLOB)):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        file_date = _file_date(path)
        file_in_window = cutoff is None or (file_date is not None and file_date >= cutoff.date())
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                if file_in_window:
                    malformed += 1
                continue
            if not isinstance(parsed, dict):
                if file_in_window:
                    malformed += 1
                continue
            ts_raw = parsed.get("timestamp")
            ts = _parse_timestamp(ts_raw) if isinstance(ts_raw, str) else None
            if ts is None:
                if file_in_window:
                    malformed += 1
                continue
            records.append((ts, parsed, stripped))

    records.sort(key=lambda triple: triple[0])

    if cutoff is not None:
        records = [t for t in records if t[0] >= cutoff]

    return LoadResult(
        records=[t[1] for t in records],
        raw_lines=[t[2] for t in records],
        malformed=malformed,
    )


# ---------------------------------------------------------------------------
# discrepancy_rate
# ---------------------------------------------------------------------------


def discrepancy_rate(
    *,
    window: str = "1h",
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
) -> float:
    """Fraction of records in the most recent ``window`` with ``arbitration_outcome == "diverge"``.

    ``shadow_only`` (shadow-side exception) and ``canonical_only`` (reserved)
    are *not* counted as discrepancies — they signal shadow infrastructure
    health, not divergence between primary and shadow. Empty window → 0.0.
    """
    delta = parse_window(window)
    result = load_records(log_dir=log_dir, since=delta, now=now)
    if not result.records:
        return 0.0
    diverge = sum(1 for r in result.records if r.get("arbitration_outcome") == "diverge")
    return diverge / len(result.records)


# ---------------------------------------------------------------------------
# checksum_consistency
# ---------------------------------------------------------------------------


def is_record_consistent(record: dict[str, Any], raw_line: str) -> bool:
    """A record is consistent iff:

    * exactly the canonical fields are present (no missing, no extra) — the
      set is derived from :class:`parallax.router.shadow.ShadowDecisionLog`
      so a future field add propagates here automatically,
    * ``schema_version`` matches the locked major version (forward-compat
      within the ``1.x`` series; ``2.0`` would correctly fail),
    * ``json.dumps(record, sort_keys=True)`` reproduces the on-disk line —
      protects the deterministic-checksum guarantee documented in the runbook.
    """
    if set(record.keys()) != _CANONICAL_FIELDS:
        return False
    sv = record.get("schema_version")
    if not isinstance(sv, str) or not sv.startswith(_SCHEMA_VERSION_PREFIX):
        return False
    return json.dumps(record, sort_keys=True) == raw_line


def checksum_consistency(
    *,
    window: str = "1h",
    log_dir: Path | str | None = None,
    now: _dt.datetime | None = None,
) -> float:
    """Fraction of records in the most recent ``window`` that are consistent.

    A record is consistent when it parses, has exactly the 9 canonical fields,
    has the locked ``schema_version``, and round-trips through
    ``json.dumps(sort_keys=True)`` to the on-disk byte form. Inconsistent or
    malformed records inside the window count against the metric; records
    outside the window are excluded entirely.

    Empty window → 1.0 (vacuous truth — no records, no inconsistency).
    Operators verifying the 72h DoD must additionally check that the record
    count is non-zero; that responsibility lives in
    ``scripts/shadow_continuity_check.py`` rather than here.
    """
    delta = parse_window(window)
    result = load_records(log_dir=log_dir, since=delta, now=now)
    total = len(result.records) + result.malformed
    if total == 0:
        return 1.0
    consistent = sum(
        1
        for record, raw in zip(result.records, result.raw_lines, strict=True)
        if is_record_consistent(record, raw)
    )
    return consistent / total


# ---------------------------------------------------------------------------
# compute_checksum_chain
# ---------------------------------------------------------------------------


def compute_checksum_chain(records: Iterable[dict[str, Any]]) -> str:
    """Return rolling SHA-256 hex digest over deterministic JSONL forms.

    For records r0, r1, ..., rn:

        h0 = sha256(jsonl(r0))
        h_i = sha256(h_{i-1}.encode() + b"\\n" + jsonl(r_i).encode())

    Returns an empty string for an empty iterator. Re-ordering or mutating
    any field produces a different final digest, so the chain is the
    primary signal for "zero log loss" in the WS-3 continuity check.
    """
    digest = ""
    for record in records:
        line = json.dumps(record, sort_keys=True)
        if not digest:
            digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
        else:
            digest = hashlib.sha256(
                digest.encode("utf-8") + b"\n" + line.encode("utf-8")
            ).hexdigest()
    return digest
