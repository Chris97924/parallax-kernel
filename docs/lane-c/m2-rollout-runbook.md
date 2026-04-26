# M2 — L1 Shadow Observability Rollout Runbook

**Lane C v0.2.0-beta**. Audience: whoever ships the two M2 branches to `main-next` and watches the 72-hour DoD window.

## Branches & Commits

| Branch | Short SHA | Scope | Tests |
|---|---|---|---|
| `feat/m2-wal` | `c7bdb95` | WS-1 Track 0 #3 — local SQLite WAL + offline-replay integration test + hook drain wiring | 16 scoped (15 passed + 1 skipped on win32); `parallax/wal.py` 100% line cov |
| `feat/lane-c-us-006-shadow-mode` | `3bff5dc` | WS-2 Lane C US-006 — `ShadowInterceptor` + 6-field `ShadowDecisionLog` + flag-gated read-only path | 11 scoped (all green); `parallax/router/shadow.py` 96% line cov |

Both branches are off `origin/main-next` at `18961b2` (H-2 fix) and verified fast-forwardable — no rebase required.

Full-suite delta vs `main-next` baseline (FAILED=15, ERROR=6 pre-existing): **0/0**. Zero regression introduced by either branch.

## Merge Order

1. **`feat/m2-wal` first**. The WAL is foundational infrastructure (offline-tolerant client) and is fully self-contained — no router code paths touched. Risk profile: very low.
   - Open PR, run CI, fast-forward squash-merge to `main-next`.
2. **`feat/lane-c-us-006-shadow-mode` second**. Shadow interceptor lands as a new module that is *not yet wired into any production caller*; it is only callable via `ShadowInterceptor(canonical, shadow_factory)` constructor that nothing imports yet.
   - Open PR, run CI, fast-forward squash-merge to `main-next`.
3. **WS-3 (Grafana / discrepancy detector / checksum, 3-day work)** ships as a follow-up branch `feat/m2-shadow-observability` once schema is field-locked in production. Schema:

```text
query_type, selected_port, crosswalk_status, arbitration_outcome, latency_ms, correlation_id
```

`to_jsonl()` uses `sort_keys=True`, so checksum chains are deterministic across writers.

## 72-hour DoD Verification

Run these against the live `parallax/logs/shadow-decisions-*.jsonl` once `SHADOW_MODE=true` has been on for at least 72 hours under real traffic.

```bash
# 1. Zero log loss — checksum chain over the full 72h JSONL window.
#    Expected: every line's hash links to the previous (no gaps).
python scripts/shadow_continuity_check.py --since=72h

# 2. discrepancy_rate <= 0.3% (rolling 1h windows)
#    Counts arbitration_outcome=='diverge' / total over each 1h slice.
python -c "from parallax.shadow.discrepancy import discrepancy_rate; print(discrepancy_rate(window='1h'))"

# 3. checksum 一致率 >= 99.9% (>= 999/1000 windows green)
#    Each rolling 1h window's checksum recomputed and compared to chain.
python -c "from parallax.shadow.discrepancy import checksum_consistency; print(checksum_consistency(window='1h'))"
```

WS-3 will provide both `scripts/shadow_continuity_check.py` and the `parallax.shadow.discrepancy` module. Until WS-3 ships, manually `wc -l` and `tail -n 1` of the JSONL files to spot-check.

## Rollback Procedure

The shadow path is gated by a single environment variable. Recovery is **flag-flip only — no redeploy**.

```bash
# On every Parallax instance running shadow:
export SHADOW_MODE=false
# Hook calls re-read os.environ.get on each request, so the next call bypasses
# the shadow path entirely. No process restart needed.
```

Belt-and-braces: emptying `SHADOW_USER_ALLOWLIST` short-circuits even if `SHADOW_MODE=true`:

```bash
export SHADOW_USER_ALLOWLIST=
```

Recovery target: **< 5 minutes** from incident detection (Grafana alert) to the next request bypassing shadow on every Parallax instance.

The hard invariants in `parallax/router/shadow.py` mean a shadow-side bug cannot corrupt user-facing data — `query()` always returns the canonical result; shadow exceptions are caught and logged as `arbitration_outcome=shadow_only` without propagating. So a flag flip is sufficient even in worst-case scenarios.

## Files Touched (for PR descriptions / change-log)

- `parallax/wal.py` (new) — `WALQueue` SQLite-backed offline queue, stdlib-only
- `tests/test_wal.py` (new) — 14 unit tests
- `tests/integration/test_wal_offline_replay.py` (new) — M2 DoD integration test (5 enqueues during outage + reconnect → zero loss)
- `plugins/parallax-session-hook/hook.py` (modified) — inlined `_WALQueue` (stdlib-only client copy) + `_drain_wal` on hook start
- `parallax/router/shadow.py` (new) — `ShadowInterceptor` + `ShadowDecisionLog`
- `tests/router/test_shadow_interceptor.py` (new) — 11 tests covering schema / bypass / divergence / latency / correlation_id
