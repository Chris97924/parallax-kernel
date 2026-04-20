"""Eval constants frozen per ADR-006 Run A baseline (2026-04-20).

Any drift beyond ``BASELINE_TOLERANCE`` in a reproduction run is a signal
to stop and git-bisect the pipeline before continuing Day-1 work.
"""

from __future__ import annotations

RUN_A_BASELINE: float = 0.860  # s_baseline Pro-judge (494Q reproduced 0.8603)
FALLBACK_FLOOR: float = 0.817  # CI gate floor for fallback_e2e
BASELINE_TOLERANCE: float = 0.01  # ±1% reproducibility window
