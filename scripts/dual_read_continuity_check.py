#!/usr/bin/env python3
"""M3b — 72h continuity check for the dual-read decision JSONL stream (US-006).

Verifies the six DoD numerics from ralplan §6 line 416-426 in one shot:

  ``discrepancy_rate``           ≤ 0.001  (0.1%)
  ``arbitration_conflict_rate``  ≤ 0.01   (1%)
  ``write_error_rate``           ≤ 0.0002 (0.02%)
  ``aphelion_unreachable_rate``  ≤ 0.005  (0.5%)
  ``crosswalk_miss_rate``        ≤ 0.05   (5%, measured at +48h gate per Q11)
  ``circuit_open_count``         ≤ 3      (absolute count over the 72h window)

Plus a record-count gate (``--min-records=N``) for "no dual-read activity =
DoD fail" semantics (mirror of the M2 shadow continuity check).

Usage::

    python scripts/dual_read_continuity_check.py --since=72h
    python scripts/dual_read_continuity_check.py --since=72h --format=json --min-records=1000

Exit code is 0 iff every assertion passes; 1 otherwise. The summary report
is written to stdout (never stderr) so the CLI is composable in pipelines.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

# Add repo root to sys.path so this script is runnable without installation.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from parallax.router.dual_read_metrics import (  # noqa: E402
    APHELION_UNREACHABLE_THRESHOLD,
    ARBITRATION_CONFLICT_RATE_THRESHOLD,
    CIRCUIT_OPEN_72H_MAX,
    CROSSWALK_MISS_THRESHOLD,
    DISCREPANCY_RATE_THRESHOLD_M3,
    WRITE_ERROR_RATE_THRESHOLD,
    aphelion_unreachable_rate,
    arbitration_conflict_rate,
    circuit_open_count,
    crosswalk_miss_rate,
    discrepancy_rate,
    load_records,
    write_error_rate,
)
from parallax.shadow.discrepancy import parse_window  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dual_read_continuity_check",
        description=(
            "M3b US-006 — verify 6-metric DoD over the dual-read decision "
            "JSONL stream (72h default window)."
        ),
    )
    parser.add_argument(
        "--since",
        default="72h",
        help="Window covered by the check (e.g. 1h, 24h, 72h, 3d). Default: 72h.",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Override DUAL_READ_LOG_DIR for the run. Default: env or parallax/logs/.",
    )
    parser.add_argument(
        "--threshold-discrepancy",
        type=float,
        default=DISCREPANCY_RATE_THRESHOLD_M3,
        help=f"Maximum tolerated discrepancy_rate. Default: {DISCREPANCY_RATE_THRESHOLD_M3}.",
    )
    parser.add_argument(
        "--threshold-conflict",
        type=float,
        default=ARBITRATION_CONFLICT_RATE_THRESHOLD,
        help=(
            f"Maximum tolerated arbitration_conflict_rate. Default: "
            f"{ARBITRATION_CONFLICT_RATE_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--threshold-write-error",
        type=float,
        default=WRITE_ERROR_RATE_THRESHOLD,
        help=f"Maximum tolerated write_error_rate. Default: {WRITE_ERROR_RATE_THRESHOLD}.",
    )
    parser.add_argument(
        "--threshold-aphelion-unreachable",
        type=float,
        default=APHELION_UNREACHABLE_THRESHOLD,
        help=(
            f"Maximum tolerated aphelion_unreachable_rate. Default: "
            f"{APHELION_UNREACHABLE_THRESHOLD}."
        ),
    )
    parser.add_argument(
        "--threshold-crosswalk-miss",
        type=float,
        default=CROSSWALK_MISS_THRESHOLD,
        help=(
            f"Maximum tolerated crosswalk_miss_rate. Default: "
            f"{CROSSWALK_MISS_THRESHOLD} (measured at +48h gate per Q11)."
        ),
    )
    parser.add_argument(
        "--threshold-circuit-open",
        type=int,
        default=CIRCUIT_OPEN_72H_MAX,
        help=(
            f"Maximum tolerated circuit_open_count over the window. Default: "
            f"{CIRCUIT_OPEN_72H_MAX}."
        ),
    )
    parser.add_argument(
        "--min-records",
        type=int,
        default=0,
        help=(
            "Minimum record count for the check to pass (zero-activity guard). "
            "Default: 0 (skip the gate when no dual-read traffic exists yet)."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("human", "json"),
        default="human",
        help="Output format. Default: human.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 UTC anchor for the window cutoff (testing/replay only).",
    )
    return parser


def _parse_now(raw: str | None) -> _dt.datetime | None:
    if raw is None:
        return None
    parsed = _dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed


def _build_report(args: argparse.Namespace) -> dict[str, Any]:
    now = _parse_now(args.now)
    log_dir = Path(args.log_dir) if args.log_dir else None

    # Load once for total_records; the rate functions reload internally.
    delta = parse_window(args.since)
    total_records = len(load_records(log_dir=log_dir, since=delta, now=now))

    rates = {
        "discrepancy_rate": discrepancy_rate(args.since, log_dir=log_dir, now=now),
        "arbitration_conflict_rate": arbitration_conflict_rate(
            args.since, log_dir=log_dir, now=now
        ),
        "write_error_rate": write_error_rate(args.since, log_dir=log_dir, now=now),
        "aphelion_unreachable_rate": aphelion_unreachable_rate(
            args.since, log_dir=log_dir, now=now
        ),
        "crosswalk_miss_rate": crosswalk_miss_rate(args.since, log_dir=log_dir, now=now),
    }
    circuit_count = circuit_open_count(args.since, log_dir=log_dir, now=now)

    failures: list[str] = []
    if total_records < args.min_records:
        failures.append("min_records")
    if rates["discrepancy_rate"] > args.threshold_discrepancy:
        failures.append("discrepancy_rate")
    if rates["arbitration_conflict_rate"] > args.threshold_conflict:
        failures.append("arbitration_conflict_rate")
    if rates["write_error_rate"] > args.threshold_write_error:
        failures.append("write_error_rate")
    if rates["aphelion_unreachable_rate"] > args.threshold_aphelion_unreachable:
        failures.append("aphelion_unreachable_rate")
    if rates["crosswalk_miss_rate"] > args.threshold_crosswalk_miss:
        failures.append("crosswalk_miss_rate")
    if circuit_count > args.threshold_circuit_open:
        failures.append("circuit_open_count")

    return {
        "since": args.since,
        "log_dir": str(log_dir) if log_dir else None,
        "total_records": total_records,
        "discrepancy_rate": rates["discrepancy_rate"],
        "arbitration_conflict_rate": rates["arbitration_conflict_rate"],
        "write_error_rate": rates["write_error_rate"],
        "aphelion_unreachable_rate": rates["aphelion_unreachable_rate"],
        "crosswalk_miss_rate": rates["crosswalk_miss_rate"],
        "circuit_open_count": circuit_count,
        "thresholds": {
            "discrepancy_rate": args.threshold_discrepancy,
            "arbitration_conflict_rate": args.threshold_conflict,
            "write_error_rate": args.threshold_write_error,
            "aphelion_unreachable_rate": args.threshold_aphelion_unreachable,
            "crosswalk_miss_rate": args.threshold_crosswalk_miss,
            "circuit_open_count": args.threshold_circuit_open,
            "min_records": args.min_records,
        },
        "failures": failures,
        "passed": not failures,
    }


def _format_human(report: dict[str, Any]) -> str:
    """Render the report as a one-screen oncall summary."""
    status = "PASS" if report["passed"] else "FAIL"
    th = report["thresholds"]
    lines = [
        f"[{status}] M3b US-006 — dual-read continuity check",
        f"  window:                       {report['since']}",
        f"  log_dir:                      {report['log_dir'] or '(env / default)'}",
        f"  total_records:                {report['total_records']}",
        f"  discrepancy_rate:             {report['discrepancy_rate']:.6f}"
        f"  (threshold {th['discrepancy_rate']})",
        f"  arbitration_conflict_rate:    {report['arbitration_conflict_rate']:.6f}"
        f"  (threshold {th['arbitration_conflict_rate']})",
        f"  write_error_rate:             {report['write_error_rate']:.6f}"
        f"  (threshold {th['write_error_rate']})",
        f"  aphelion_unreachable_rate:    {report['aphelion_unreachable_rate']:.6f}"
        f"  (threshold {th['aphelion_unreachable_rate']})",
        f"  crosswalk_miss_rate:          {report['crosswalk_miss_rate']:.6f}"
        f"  (threshold {th['crosswalk_miss_rate']}; measured at +48h gate)",
        f"  circuit_open_count:           {report['circuit_open_count']}"
        f"  (threshold {th['circuit_open_count']})",
    ]
    if report["failures"]:
        lines.append(f"  failures:                     {', '.join(report['failures'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    report = _build_report(args)
    if args.format == "json":
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_format_human(report) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
