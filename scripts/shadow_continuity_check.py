#!/usr/bin/env python3
"""WS-3 — 72h continuity check for the shadow JSONL decision-log stream.

Verifies the three Lane C v0.2.0-beta DoD numerics in one shot:

  zero log loss            → ``--min-records`` (operator-supplied lower bound)
                            + chain hash for downstream comparison
  ``discrepancy_rate``     → ``≤ 0.003`` over the most recent ``--since`` window
  ``checksum_consistency`` → ``≥ 0.999`` over the same window

Usage::

    python scripts/shadow_continuity_check.py --since=72h
    python scripts/shadow_continuity_check.py --since=72h --format=json --min-records=1000

Exit code is 0 iff every assertion passes; 1 otherwise. The summary report is
written to stdout (never stderr) so the CLI is composable in pipelines.
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

from parallax.shadow.discrepancy import (  # noqa: E402
    CHECKSUM_CONSISTENCY_THRESHOLD,
    DISCREPANCY_RATE_THRESHOLD,
    checksum_consistency,
    compute_checksum_chain,
    discrepancy_rate,
    load_records,
    parse_window,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shadow_continuity_check",
        description=(
            "Lane C WS-3 — verify zero log loss + discrepancy_rate + "
            "checksum_consistency over the shadow JSONL stream."
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
        help="Override SHADOW_LOG_DIR for the run. Default: env or parallax/logs/.",
    )
    parser.add_argument(
        "--threshold-discrepancy",
        type=float,
        default=DISCREPANCY_RATE_THRESHOLD,
        help=(
            f"Maximum tolerated discrepancy_rate. Default: "
            f"{DISCREPANCY_RATE_THRESHOLD} (runbook DoD)."
        ),
    )
    parser.add_argument(
        "--threshold-checksum",
        type=float,
        default=CHECKSUM_CONSISTENCY_THRESHOLD,
        help=(
            f"Minimum tolerated checksum_consistency. Default: "
            f"{CHECKSUM_CONSISTENCY_THRESHOLD} (runbook DoD)."
        ),
    )
    parser.add_argument(
        "--min-records",
        type=int,
        default=0,
        help="Minimum record count for the check to pass (zero-log-loss guard).",
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
    delta = parse_window(args.since)
    log_dir = Path(args.log_dir) if args.log_dir else None

    loaded = load_records(log_dir=log_dir, since=delta, now=now)
    total = len(loaded.records) + loaded.malformed
    divergent = sum(1 for r in loaded.records if r.get("arbitration_outcome") == "diverge")
    rate = discrepancy_rate(window=args.since, log_dir=log_dir, now=now)
    consistency = checksum_consistency(window=args.since, log_dir=log_dir, now=now)
    chain = compute_checksum_chain(loaded.records)

    failures: list[str] = []
    if total < args.min_records:
        failures.append("min_records")
    if rate > args.threshold_discrepancy:
        failures.append("discrepancy_rate")
    if consistency < args.threshold_checksum:
        failures.append("checksum_consistency")

    return {
        "since": args.since,
        "log_dir": str(log_dir) if log_dir else None,
        "total_records": total,
        "parsed_records": len(loaded.records),
        "malformed": loaded.malformed,
        "divergent": divergent,
        "discrepancy_rate": rate,
        "checksum_consistency": consistency,
        "chain_hash": chain,
        "thresholds": {
            "discrepancy_rate": args.threshold_discrepancy,
            "checksum_consistency": args.threshold_checksum,
            "min_records": args.min_records,
        },
        "failures": failures,
        "passed": not failures,
    }


def _format_human(report: dict) -> str:
    """Render the report as a one-screen summary for oncall."""
    status = "PASS" if report["passed"] else "FAIL"
    lines = [
        f"[{status}] Lane C WS-3 — shadow continuity check",
        f"  window:                 {report['since']}",
        f"  log_dir:                {report['log_dir'] or '(env / default)'}",
        f"  total_records:          {report['total_records']}",
        f"    parsed:               {report['parsed_records']}",
        f"    malformed:            {report['malformed']}",
        f"  divergent:              {report['divergent']}",
        f"  discrepancy_rate:       {report['discrepancy_rate']:.6f}"
        f"  (threshold {report['thresholds']['discrepancy_rate']})",
        f"  checksum_consistency:   {report['checksum_consistency']:.6f}"
        f"  (threshold {report['thresholds']['checksum_consistency']})",
        f"  chain_hash:             {report['chain_hash'] or '(empty)'}",
    ]
    if report["failures"]:
        lines.append(f"  failures:               {', '.join(report['failures'])}")
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
