# M3 Gate C Smoke — DUAL_READ flag check point

**Date:** 2026-04-30
**Branch:** `feat/m3b-arbitration` (off `origin/main-next` head `5e618fd`)
**Scope:** Verify `DUAL_READ` env flag wiring + per-request middleware snapshot.
**Out of scope:** Real Aphelion HTTP adapter (M4 — see `aphelion_stub.py` docstring).

---

## What Gate C verifies

1. **`DUAL_READ=false` (default)**: `DualReadRouter.query()` returns `primary_only` outcome — secondary read NOT attempted.
2. **`DUAL_READ=true`**: router fires both reads (against the M3a stub which always raises `AphelionUnreachableError`); arbitration wires through `LiveArbitrationDecision`; outcome classifies as `primary_only_due_to_unreachable` (or equivalent fallback per Story 4 contract).
3. **Per-request snapshot middleware**: `request.state.dual_read` honored over env mutation mid-flight (verified by `parallax/server/middleware/dual_read_snapshot.py` + integration test).
4. **Story 4 invariant preserved**: after dual-attempt, `LiveArbitrationDecision` attached to `DualReadResult.arbitration` field; `winning_source == "fallback"` when secondary unreachable.

## Existing test coverage

The M3a test suite already exercises both flag states. With Story 4 wiring in place, additional integration coverage attached:

```
tests/router/test_dual_read_router.py
  - test_dual_read_disabled_returns_primary_only          # DUAL_READ=false path
  - test_dual_read_enabled_dispatches_secondary           # DUAL_READ=true path
  - test_dual_read_aphelion_unreachable_falls_back        # stub-raise → fallback
  - test_dual_read_request_state_snapshot_honored         # middleware override
  - test_dual_read_arbitration_attached_for_parallax_owned # Story 4 integration
  - test_dual_read_arbitration_attached_for_aphelion_owned # Story 4 integration
  - test_dual_read_arbitration_attached_for_fallback       # Story 4 integration
  - test_dual_read_arbitration_skipped_when_no_secondary   # Story 4 integration

tests/server/middleware/test_dual_read_snapshot.py
  - test_per_request_flag_overrides_env
  - test_per_request_flag_immune_to_mid_flight_env_mutation
```

## Reproduction commands

Run after `feat/m3b-arbitration` branch is fully built (post-Story 6 merge ready) so the metrics layer is also wired:

```bash
cd E:/Parallax
git checkout feat/m3b-arbitration

# Targeted DUAL_READ flag tests
DUAL_READ=false python -m pytest tests/router/test_dual_read_router.py --no-cov -v -k "disabled or primary_only"
DUAL_READ=true  python -m pytest tests/router/test_dual_read_router.py --no-cov -v -k "enabled or unreachable or arbitration"

# Middleware snapshot tests
python -m pytest tests/server/middleware/test_dual_read_snapshot.py --no-cov -v

# Full router suite (regression baseline)
python -m pytest tests/router/ --no-cov
```

Expected exit: **all PASS, 0 fail, 0 error.**

## Smoke results — Baseline at branch tip `928128c` (post-Story-4)

Captured during Autopilot 2026-04-30 while Story 5 (M3-T2.2 conflict_writer) executor is running. Story 5 will add ≥10 unit tests + 1 integration test that emits conflict events; Story 6 will add ≥6 metrics tests + ≥8 CLI tests. Re-run smoke at post-Story-6 tip and append delta below.

```text
Branch tip:                  928128c (post-Story-4 stable)
Run timestamp (UTC):         2026-04-30T07:xx:00Z

DUAL_READ=false suite:       55 passed
  pytest tests/router/test_dual_read_router.py --no-cov

DUAL_READ=true  suite:       55 passed
  pytest tests/router/test_dual_read_router.py --no-cov  (same tests, env override)

Middleware snapshot suite:   9 passed
  pytest tests/server/test_dual_read_snapshot_middleware.py --no-cov

Full router suite:           445 passed, 4 xfailed (no failures, no errors)
  pytest tests/router/ --no-cov

Coverage on parallax/router/live_arbitration.py: 100% (Story 4)
ruff/black:                  clean (Story 4)
```

### Delta after Story 5/6 — TO APPEND

```text
[PLACEHOLDER — fill after Story 6 commit]
- DUAL_READ=false delta:   55 → ___ passed (+__ from Story 5/6)
- DUAL_READ=true  delta:   55 → ___ passed (+__ from Story 5/6)
- Middleware suite delta:  9 → ___ passed
- Full router suite delta: 445 → ___ passed
- New conflict-event emission integration test pass: ___
- New metrics scrape integration test pass: ___
- Final branch tip: ___
```

## Sign-off conditions

Gate C is verified when:

- All listed tests pass for both flag states.
- No regressions vs `origin/main-next` baseline (current 1276 passed).
- Notion devlog 2026-04-30 page updated with this artifact link.

## Notes

- **Story 1 (Gate B Aphelion endpoint smoke) DROPPED 2026-04-30**: real Aphelion HTTP adapter is M4 scope; current `aphelion_stub.py` always raises by design. Gate C subsumes the relevant fallback verification.
- Real DoD verification (Orbit M6 Commit B canary + 14-day corpus + 72h window) remains outside Gate C scope — tracked in M3 plan §6.
