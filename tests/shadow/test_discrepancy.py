"""WS-3 discrepancy detector + checksum-chain TDD coverage.

Specifies the contract for ``parallax.shadow.discrepancy``:
- ``discrepancy_rate(window='1h')`` — fraction of records in the most recent
  ``window`` whose ``arbitration_outcome == "diverge"``.
- ``checksum_consistency(window='1h')`` — fraction of records in the most
  recent ``window`` that are well-formed (parseable, 9 fields, schema_version
  match, round-trip stable).
- ``compute_checksum_chain`` — deterministic rolling SHA-256 over JSONL records.

Thresholds (per ``docs/lane-c/m2-rollout-runbook.md``):
- ``DISCREPANCY_RATE_THRESHOLD = 0.003`` (0.3%)
- ``CHECKSUM_CONSISTENCY_THRESHOLD = 0.999`` (99.9%)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

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
from tests.shadow.conftest import make_record as _record
from tests.shadow.conftest import write_records as _write_records

UTC = dt.UTC


def _now(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_thresholds_match_runbook() -> None:
    """Pinned to runbook DoD numerics — changing either breaks the contract."""
    assert DISCREPANCY_RATE_THRESHOLD == 0.003
    assert CHECKSUM_CONSISTENCY_THRESHOLD == 0.999


def test_schema_version_re_exported() -> None:
    """SCHEMA_VERSION must mirror parallax.router.shadow.SCHEMA_VERSION."""
    from parallax.router.shadow import SCHEMA_VERSION as ROUTER_SV

    assert SCHEMA_VERSION == ROUTER_SV


# ---------------------------------------------------------------------------
# parse_window
# ---------------------------------------------------------------------------


def test_parse_window_hours() -> None:
    assert parse_window("1h") == dt.timedelta(hours=1)
    assert parse_window("72h") == dt.timedelta(hours=72)


def test_parse_window_minutes() -> None:
    assert parse_window("30m") == dt.timedelta(minutes=30)


def test_parse_window_days() -> None:
    assert parse_window("3d") == dt.timedelta(days=3)


def test_parse_window_seconds() -> None:
    assert parse_window("90s") == dt.timedelta(seconds=90)


def test_parse_window_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        parse_window("invalid")
    with pytest.raises(ValueError):
        parse_window("")
    with pytest.raises(ValueError):
        parse_window("0")  # bare integer is ambiguous


def test_parse_window_rejects_zero_or_negative() -> None:
    with pytest.raises(ValueError):
        parse_window("0h")
    with pytest.raises(ValueError):
        parse_window("-5m")


# ---------------------------------------------------------------------------
# load_records
# ---------------------------------------------------------------------------


def test_load_records_empty_dir(log_dir: Path) -> None:
    result = load_records(log_dir=log_dir)
    assert result.records == []
    assert result.malformed == 0


def test_load_records_well_formed(log_dir: Path) -> None:
    _write_records(log_dir, [_record(), _record(arbitration_outcome="diverge")])
    result = load_records(log_dir=log_dir)
    assert len(result.records) == 2
    assert result.malformed == 0


def test_load_records_skips_blank_lines(log_dir: Path) -> None:
    path = _write_records(log_dir, [_record()])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n")
    result = load_records(log_dir=log_dir)
    assert len(result.records) == 1


def test_load_records_counts_malformed(log_dir: Path) -> None:
    path = _write_records(log_dir, [_record()])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    result = load_records(log_dir=log_dir)
    assert len(result.records) == 1
    assert result.malformed == 1


def test_load_records_filters_by_since(log_dir: Path) -> None:
    _write_records(
        log_dir,
        [
            _record(timestamp="2026-04-26T08:00:00.000000+00:00"),  # 4h ago
            _record(timestamp="2026-04-26T11:30:00.000000+00:00"),  # 30m ago
        ],
    )
    result = load_records(
        log_dir=log_dir,
        since=dt.timedelta(hours=1),
        now=_now("2026-04-26T12:00:00+00:00"),
    )
    assert len(result.records) == 1
    assert result.records[0]["timestamp"].startswith("2026-04-26T11:30")


def test_load_records_merges_multiple_daily_files(log_dir: Path) -> None:
    _write_records(
        log_dir, [_record(timestamp="2026-04-25T23:30:00.000000+00:00")], date="2026-04-25"
    )
    _write_records(
        log_dir, [_record(timestamp="2026-04-26T00:30:00.000000+00:00")], date="2026-04-26"
    )
    result = load_records(log_dir=log_dir)
    assert len(result.records) == 2


# ---------------------------------------------------------------------------
# discrepancy_rate
# ---------------------------------------------------------------------------


def test_discrepancy_rate_empty_log_dir(log_dir: Path) -> None:
    """No records → 0.0 (no observed discrepancy)."""
    rate = discrepancy_rate(window="1h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert rate == 0.0


def test_discrepancy_rate_all_match(log_dir: Path) -> None:
    records = [
        _record(arbitration_outcome="match", timestamp=f"2026-04-26T10:{i:02d}:00.000000+00:00")
        for i in range(10)
    ]
    _write_records(log_dir, records)
    rate = discrepancy_rate(window="1h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert rate == 0.0


def test_discrepancy_rate_all_diverge(log_dir: Path) -> None:
    records = [
        _record(arbitration_outcome="diverge", timestamp=f"2026-04-26T10:{i:02d}:00.000000+00:00")
        for i in range(5)
    ]
    _write_records(log_dir, records)
    rate = discrepancy_rate(window="1h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert rate == 1.0


def test_discrepancy_rate_passes_dod_at_threshold(log_dir: Path) -> None:
    """1 diverge in 1000 records (0.1%) is well below 0.3% DoD."""
    records = [
        _record(arbitration_outcome="match", timestamp="2026-04-26T10:30:00.000000+00:00")
        for _ in range(999)
    ]
    records.append(
        _record(arbitration_outcome="diverge", timestamp="2026-04-26T10:30:00.000000+00:00")
    )
    _write_records(log_dir, records)
    rate = discrepancy_rate(window="1h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert abs(rate - 0.001) < 1e-9
    assert rate <= DISCREPANCY_RATE_THRESHOLD


def test_discrepancy_rate_fails_dod_above_threshold(log_dir: Path) -> None:
    """4 diverge in 1000 records (0.4%) exceeds 0.3% DoD."""
    records = [
        _record(arbitration_outcome="match", timestamp="2026-04-26T10:30:00.000000+00:00")
        for _ in range(996)
    ]
    records.extend(
        _record(arbitration_outcome="diverge", timestamp="2026-04-26T10:30:00.000000+00:00")
        for _ in range(4)
    )
    _write_records(log_dir, records)
    rate = discrepancy_rate(window="1h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert rate > DISCREPANCY_RATE_THRESHOLD


def test_discrepancy_rate_excludes_records_outside_window(log_dir: Path) -> None:
    """Records older than window must not count toward the rate."""
    records = [
        _record(arbitration_outcome="match", timestamp="2026-04-26T10:30:00.000000+00:00"),
        # 2h ago — outside 1h window
        _record(arbitration_outcome="diverge", timestamp="2026-04-26T09:00:00.000000+00:00"),
    ]
    _write_records(log_dir, records)
    rate = discrepancy_rate(window="1h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert rate == 0.0


def test_discrepancy_rate_shadow_only_does_not_count(log_dir: Path) -> None:
    """``shadow_only`` is a shadow-side error, not a divergence between primary/shadow."""
    records = [
        _record(arbitration_outcome="match", timestamp="2026-04-26T10:30:00.000000+00:00"),
        _record(arbitration_outcome="shadow_only", timestamp="2026-04-26T10:31:00.000000+00:00"),
    ]
    _write_records(log_dir, records)
    rate = discrepancy_rate(window="1h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert rate == 0.0


def test_discrepancy_rate_canonical_only_does_not_count(log_dir: Path) -> None:
    """``canonical_only`` is reserved for future fall-back scenarios; not a discrepancy."""
    records = [
        _record(arbitration_outcome="canonical_only", timestamp="2026-04-26T10:30:00.000000+00:00"),
    ]
    _write_records(log_dir, records)
    rate = discrepancy_rate(window="1h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert rate == 0.0


def test_discrepancy_rate_multi_day_24h_window(log_dir: Path) -> None:
    """Records spanning two daily files within a 24h window are merged correctly."""
    _write_records(
        log_dir,
        [_record(arbitration_outcome="match", timestamp="2026-04-25T23:30:00.000000+00:00")],
        date="2026-04-25",
    )
    _write_records(
        log_dir,
        [_record(arbitration_outcome="diverge", timestamp="2026-04-26T00:30:00.000000+00:00")],
        date="2026-04-26",
    )
    rate = discrepancy_rate(window="24h", log_dir=log_dir, now=_now("2026-04-26T01:00:00+00:00"))
    assert abs(rate - 0.5) < 1e-9


def test_discrepancy_rate_default_log_dir_from_env(
    log_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``log_dir`` is None, fall back to ``SHADOW_LOG_DIR`` env."""
    _write_records(
        log_dir,
        [_record(arbitration_outcome="diverge", timestamp="2026-04-26T10:30:00.000000+00:00")],
    )
    monkeypatch.setenv("SHADOW_LOG_DIR", str(log_dir))
    rate = discrepancy_rate(window="1h", now=_now("2026-04-26T11:00:00+00:00"))
    assert rate == 1.0


# ---------------------------------------------------------------------------
# checksum_consistency
# ---------------------------------------------------------------------------


def test_checksum_consistency_empty_dir(log_dir: Path) -> None:
    """Empty window → 1.0 (vacuous: no records, no inconsistency)."""
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 1.0


def test_checksum_consistency_all_well_formed(log_dir: Path) -> None:
    records = [_record(timestamp=f"2026-04-26T11:{m:02d}:00.000000+00:00") for m in range(0, 60, 5)]
    _write_records(log_dir, records)
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 1.0


def test_checksum_consistency_one_malformed_in_window(log_dir: Path) -> None:
    """One unparseable line in 1000 → 0.999 (just at threshold)."""
    records = [
        _record(timestamp=f"2026-04-26T11:{i // 60:02d}:{i % 60:02d}.000000+00:00")
        for i in range(999)
    ]
    path = _write_records(log_dir, records)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("garbage line not json\n")
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert abs(consistency - 0.999) < 1e-9
    assert consistency >= CHECKSUM_CONSISTENCY_THRESHOLD


def test_checksum_consistency_missing_field_fails(log_dir: Path) -> None:
    """Record missing one of 9 fields counts as inconsistent."""
    bad = _record(timestamp="2026-04-26T11:00:00.000000+00:00")
    bad.pop("user_id")
    _write_records(log_dir, [bad])
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 0.0


def test_checksum_consistency_extra_field_fails(log_dir: Path) -> None:
    """Record with an unknown extra field violates the locked 9-field schema."""
    _write_records(
        log_dir,
        [_record(timestamp="2026-04-26T11:00:00.000000+00:00", unknown_field="x")],
    )
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 0.0


def test_checksum_consistency_wrong_schema_version_fails(log_dir: Path) -> None:
    """schema_version must equal SCHEMA_VERSION; otherwise inconsistent."""
    _write_records(
        log_dir,
        [_record(timestamp="2026-04-26T11:00:00.000000+00:00", schema_version="0.9")],
    )
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 0.0


def test_checksum_consistency_non_sorted_keys_fails(log_dir: Path) -> None:
    """A line not in sort_keys=True canonical form breaks the deterministic-checksum guarantee."""
    record = _record(timestamp="2026-04-26T11:00:00.000000+00:00")
    path = log_dir / "shadow-decisions-2026-04-26.jsonl"
    # Write with sort_keys=False — should fail round-trip stability.
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=False) + "\n")
    # Force the write to actually NOT be sort-keys form — pick a key order
    # that differs from alphabetical. Direct dict literal preserves insertion order.
    with path.open("w", encoding="utf-8") as fh:
        # Reverse-alphabetical insertion so json.dumps(default) doesn't match sort_keys=True.
        rec = {
            "user_id": record["user_id"],
            "timestamp": record["timestamp"],
            "selected_port": record["selected_port"],
            "schema_version": record["schema_version"],
            "query_type": record["query_type"],
            "latency_ms": record["latency_ms"],
            "crosswalk_status": record["crosswalk_status"],
            "correlation_id": record["correlation_id"],
            "arbitration_outcome": record["arbitration_outcome"],
        }
        fh.write(json.dumps(rec) + "\n")
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 0.0


def test_checksum_consistency_excludes_records_outside_window(log_dir: Path) -> None:
    """Bad records outside the window do not pollute the metric."""
    good = _record(timestamp="2026-04-26T11:30:00.000000+00:00")
    bad = _record(timestamp="2026-04-26T09:00:00.000000+00:00")
    bad.pop("user_id")
    _write_records(log_dir, [good, bad])
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 1.0


# ---------------------------------------------------------------------------
# compute_checksum_chain
# ---------------------------------------------------------------------------


def test_compute_checksum_chain_empty() -> None:
    assert compute_checksum_chain([]) == ""


def test_compute_checksum_chain_single_record() -> None:
    record = _record()
    chain = compute_checksum_chain([record])
    expected = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    assert chain == expected


def test_compute_checksum_chain_deterministic() -> None:
    """Same records → same chain hash, regardless of dict key insertion order."""
    r1 = _record()
    r2 = _record(arbitration_outcome="diverge")
    chain_a = compute_checksum_chain([r1, r2])
    # Re-built dicts with different key order — should still hash identically.
    r1_reordered = {k: r1[k] for k in reversed(list(r1.keys()))}
    r2_reordered = {k: r2[k] for k in reversed(list(r2.keys()))}
    chain_b = compute_checksum_chain([r1_reordered, r2_reordered])
    assert chain_a == chain_b


def test_compute_checksum_chain_changes_on_any_field() -> None:
    """A single field change must propagate through the rolling chain."""
    r1 = _record()
    r2_same = _record(arbitration_outcome="match")
    r2_diff = _record(arbitration_outcome="diverge")
    assert compute_checksum_chain([r1, r2_same]) != compute_checksum_chain([r1, r2_diff])


def test_compute_checksum_chain_order_sensitive() -> None:
    """Reordering records changes the chain hash — log loss must show up here."""
    r1 = _record(correlation_id="a")
    r2 = _record(correlation_id="b")
    assert compute_checksum_chain([r1, r2]) != compute_checksum_chain([r2, r1])


# ---------------------------------------------------------------------------
# Iter 2 fixes — regression tests for reviewer findings
# ---------------------------------------------------------------------------


def test_load_records_naive_timestamp_normalised_to_utc(log_dir: Path) -> None:
    """CRITICAL regression: a naive timestamp must NOT crash load_records.

    A record with a timestamp lacking a tz offset (e.g. ``2026-04-26T10:30:00``)
    used to raise TypeError on the cutoff comparison because the cutoff is
    UTC-aware. The fix forces UTC on naive results — the documented default
    for daily file rotation.
    """
    naive = _record(timestamp="2026-04-26T10:30:00")  # NOTE: no +00:00
    _write_records(log_dir, [naive])
    result = load_records(
        log_dir=log_dir,
        since=dt.timedelta(hours=24),
        now=_now("2026-04-26T11:00:00+00:00"),
    )
    # Should load, not crash. The naive timestamp is interpreted as UTC.
    assert len(result.records) == 1
    assert result.malformed == 0


def test_discrepancy_rate_naive_timestamp_does_not_crash(log_dir: Path) -> None:
    """Companion: discrepancy_rate must not crash on naive timestamps."""
    _write_records(
        log_dir,
        [_record(arbitration_outcome="diverge", timestamp="2026-04-26T10:30:00")],
    )
    rate = discrepancy_rate(window="24h", log_dir=log_dir, now=_now("2026-04-26T11:00:00+00:00"))
    assert rate == 1.0


def test_checksum_consistency_accepts_forward_compat_schema_version(log_dir: Path) -> None:
    """Schema bumps within ``1.x`` must remain consistent (forward-compat)."""
    _write_records(
        log_dir,
        [_record(timestamp="2026-04-26T11:00:00.000000+00:00", schema_version="1.5")],
    )
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 1.0


def test_checksum_consistency_rejects_major_version_bump(log_dir: Path) -> None:
    """``2.0`` (or any non-``1.x``) must surface as inconsistent immediately."""
    _write_records(
        log_dir,
        [_record(timestamp="2026-04-26T11:00:00.000000+00:00", schema_version="2.0")],
    )
    consistency = checksum_consistency(
        window="1h", log_dir=log_dir, now=_now("2026-04-26T12:00:00+00:00")
    )
    assert consistency == 0.0


def test_resolve_log_dir_returns_absolute_path(
    log_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense-in-depth: SHADOW_LOG_DIR must always resolve to an absolute path."""
    from parallax.shadow.discrepancy import _resolve_log_dir

    # Explicit param
    out = _resolve_log_dir(log_dir)
    assert out.is_absolute()

    # Env var
    monkeypatch.setenv("SHADOW_LOG_DIR", str(log_dir))
    out_env = _resolve_log_dir(None)
    assert out_env.is_absolute()

    # Default fallback
    monkeypatch.delenv("SHADOW_LOG_DIR", raising=False)
    out_default = _resolve_log_dir(None)
    assert out_default.is_absolute()
    # Default points inside the project tree (mirrors parallax.config default)
    assert out_default.name == "logs"


def test_default_log_dir_matches_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drift guard: parallax.shadow.discrepancy default must match parallax.config."""
    from parallax.config import load_config
    from parallax.shadow.discrepancy import _DEFAULT_LOG_DIR

    for key in ("SHADOW_MODE", "SHADOW_USER_ALLOWLIST", "SHADOW_LOG_DIR"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config()
    assert _DEFAULT_LOG_DIR.resolve() == cfg.shadow_log_dir


def test_canonical_fields_derived_from_dataclass() -> None:
    """Drift guard: _CANONICAL_FIELDS must mirror ShadowDecisionLog dataclass.

    If parallax.router.shadow.ShadowDecisionLog gains a 10th field, this
    test still passes (set is derived). It pins the count and member set so
    hand-edited copies in code or tests can't silently drift.
    """
    import dataclasses

    from parallax.router.shadow import ShadowDecisionLog
    from parallax.shadow.discrepancy import _CANONICAL_FIELDS

    expected = frozenset(f.name for f in dataclasses.fields(ShadowDecisionLog))
    assert _CANONICAL_FIELDS == expected
    # As of v1.0 the contract is exactly 9 fields. A schema bump should also
    # bump SCHEMA_VERSION; this assertion catches a silent field add.
    assert len(_CANONICAL_FIELDS) == 9


def test_is_record_consistent_public_predicate() -> None:
    """`is_record_consistent` is the public predicate metrics.py reuses."""
    from parallax.shadow.discrepancy import is_record_consistent

    record = _record(timestamp="2026-04-26T11:00:00.000000+00:00")
    raw = json.dumps(record, sort_keys=True)
    assert is_record_consistent(record, raw) is True

    # Missing field
    bad = dict(record)
    bad.pop("user_id")
    raw_bad = json.dumps(bad, sort_keys=True)
    assert is_record_consistent(bad, raw_bad) is False

    # Mutated raw line that doesn't round-trip
    assert is_record_consistent(record, raw + " ") is False
