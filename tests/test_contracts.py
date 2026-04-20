"""Contract tests for parallax.retrieval.contracts.

The priority order isn't a free parameter — ADR-006 §1 defines it. This test
parses the ADR priority table and pins INTENT_PRIORITY to it, so a silent
reorder in either place is caught.
"""

from __future__ import annotations

import pathlib
import re

from parallax.retrieval.contracts import INTENT_PRIORITY, Intent

ADR_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "docs"
    / "adr"
    / "ADR-006-retrieval-filtered-pipeline.md"
)


def _parse_adr_priority_order() -> list[str]:
    """Parse the priority table in ADR-006 §1 and return intent names in order."""
    text = ADR_PATH.read_text(encoding="utf-8")
    # Locate the §1 table (six rows, numeric priority column).
    rows: list[tuple[int, str]] = []
    for line in text.splitlines():
        # Match "| <digit> | `intent` | ..."
        m = re.match(r"\|\s*(\d+)\s*\|\s*`([a-z_]+)`", line)
        if m:
            rows.append((int(m.group(1)), m.group(2)))
    # Keep first six (the §1 table); stop at the first gap in priority numbering.
    rows.sort(key=lambda r: r[0])
    seen: set[int] = set()
    ordered: list[str] = []
    for prio, name in rows:
        if prio in seen:
            continue
        seen.add(prio)
        ordered.append(name)
        if len(ordered) == 6:
            break
    assert len(ordered) == 6, f"expected 6 intents in ADR §1 table, got {ordered}"
    return ordered


def test_intent_priority_matches_adr():
    parsed = _parse_adr_priority_order()
    code_order = [i.value for i in INTENT_PRIORITY]
    assert code_order == parsed, (
        f"INTENT_PRIORITY order {code_order} does not match ADR-006 §1 {parsed}"
    )


def test_intent_priority_is_closed_set():
    # Every enum member shows up exactly once in the priority tuple.
    assert set(INTENT_PRIORITY) == set(Intent)
    assert len(INTENT_PRIORITY) == len(Intent)
