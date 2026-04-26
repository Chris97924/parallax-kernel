# M2 — L1 Shadow Observability Rollout Runbook

**Lane C v0.2.0-beta**. Audience: whoever ships the M2 branches to `main-next` and watches the 72-hour DoD window.

## Branches & Commits

| Branch | Short SHA | Scope | Tests |
|---|---|---|---|
| `feat/m2-wal` | `ac0add0` | WS-1 Track 0 #3 — local SQLite WAL + offline-replay integration test + hook drain wiring + eviction telemetry | 18 scoped (17 passed + 1 skipped on win32); `parallax/wal.py` 100% line cov |
| `feat/lane-c-us-006-shadow-mode` | `b38c8d8` | WS-2 Lane C US-006 — `ShadowInterceptor` + 9-field `ShadowDecisionLog` + flag-gated read-only path | 17 scoped (all green); `parallax/router/shadow.py` ≥ 96% line cov |
| `feat/m2-rollout-runbook` | (this branch) | docs only — this runbook | n/a |

Commit history (Iter 2 = post-architect-review fixes):
- `feat/m2-wal`: `c7bdb95` (Iter 1) → `ac0add0` (Iter 2 eviction telemetry)
- `feat/lane-c-us-006-shadow-mode`: `3bff5dc` (Iter 1) → `b38c8d8` (Iter 2 schema lock-in + invariants)

Both branches are off `origin/main-next` at `18961b2` (H-2 fix) and verified fast-forwardable — no rebase required.

Full-suite delta vs `main-next` baseline (FAILED=15, ERROR=6 pre-existing): **0/0**. Zero regression introduced.

## Decision Log Schema (9-field, locked at v1.0)

WS-3 will refuse to ingest unknown schemas, so **renaming or removing a field is a breaking change**. Adding a field requires bumping `schema_version`.

```text
arbitration_outcome   query_type           timestamp
correlation_id        schema_version       user_id
crosswalk_status      selected_port
latency_ms
```

`to_jsonl()` uses `sort_keys=True` so checksum chains are deterministic across writers. `timestamp` is ISO-8601 UTC with microsecond precision; daily file rotation also uses UTC.

## Merge Order

1. **`feat/m2-wal` first**. The WAL is foundational infrastructure (offline-tolerant client) and is fully self-contained — no router code paths touched. Risk profile: very low.
   - Open PR, run CI, fast-forward squash-merge to `main-next`.
2. **`feat/lane-c-us-006-shadow-mode` second**. Shadow interceptor lands as a new module that is *not yet wired into any production caller*; it is only callable via `ShadowInterceptor(canonical, shadow_factory)` constructor that nothing imports yet. Verify this stays true at merge time:
   ```bash
   git -C /e/Parallax show b38c8d8 -- 'parallax/' | grep -E "from parallax\.router\.shadow|import.*ShadowInterceptor"
   # expect: only the test file imports it
   ```
3. **`feat/m2-rollout-runbook` (this doc)** — docs-only, can merge any time.
4. **WS-3 (Grafana / discrepancy detector / checksum, 3-day work)** ships as a follow-up branch `feat/m2-shadow-observability` once schema is field-locked in production.

## Post-merge Enablement

Shadow does **nothing** in production until both flags are set. Default state after merge: fully bypassed (zero overhead).

**Where:** ZenBook (`chris@192.168.1.111`), Parallax v0.6 service env file. Per `project_parallax_zenbook_deploy` memory the deploy is a systemd unit; flag flips need a service restart-on-edit OR re-export inside the unit's environment file.

**Who:** Chris flips the flag manually. Not part of CI / not auto-rolled.

**Allowlist:** start with one user (Chris's primary `user_id`), expand only after the 72h DoD window confirms zero regression.

```bash
# On ZenBook, edit the systemd EnvironmentFile (path per current deploy):
SHADOW_MODE=true
SHADOW_USER_ALLOWLIST=chris
SHADOW_LOG_DIR=/var/log/parallax/shadow   # absolute — see "Log directory" below

# Reload + restart so the daemon picks up the new env:
sudo systemctl daemon-reload
sudo systemctl restart parallax
```

`SHADOW_MODE` is read per-request via `os.environ.get` inside `parallax.router.shadow._is_enabled`, so a *flag flip while the process is running* takes effect on the next request — but the `SHADOW_LOG_DIR` is cached at `ShadowInterceptor.__init__`, so changing the log path requires a restart.

### Log directory

`SHADOW_LOG_DIR` defaults to `parallax/logs/` (cwd-relative). On the ZenBook deploy `cwd` is the systemd unit's `WorkingDirectory`, which may not have the right permissions. **Set an absolute path** (e.g. `/var/log/parallax/shadow`) at enablement time and ensure the service user has write access.

### Smoke test (do this once after the flag flip)

```bash
# 1. Trigger one allowlisted query through the normal session-hook path
parallax query --user chris --query-type recent_context --limit 5

# 2. Verify exactly one new line in today's JSONL
tail -n 1 /var/log/parallax/shadow/shadow-decisions-$(date -u +%F).jsonl

# Expected: a JSON object with all 9 fields and arbitration_outcome of
# "match" / "diverge" / "shadow_only". If file does not exist, log dir
# is wrong or SHADOW_USER_ALLOWLIST does not include the test user.
```

## 72-hour DoD Verification

> **NOTE:** The DoD checks below depend on the **WS-3** module (`parallax.shadow.discrepancy`) and the `scripts/shadow_continuity_check.py` CLI. Until WS-3 ships, the DoD numerics (`discrepancy_rate ≤ 0.3 %`, `checksum 一致率 ≥ 99.9 %`) **cannot be programmatically verified** — only spot-checked. Use the fallback below.

### Fallback (until WS-3 ships)
```bash
# Line count over the 72h window — sanity check that records are arriving
ls -la /var/log/parallax/shadow/shadow-decisions-*.jsonl
wc -l /var/log/parallax/shadow/shadow-decisions-*.jsonl

# Eyeball the latest record
tail -n 1 /var/log/parallax/shadow/shadow-decisions-$(date -u +%F).jsonl | python -m json.tool
```

This is **not** the DoD verification — it is a stand-in until WS-3 lands.

### After WS-3 ships
```bash
# 1. Zero log loss — checksum chain over the full 72h JSONL window
python scripts/shadow_continuity_check.py --since=72h

# 2. discrepancy_rate <= 0.3% (rolling 1h windows)
python -c "from parallax.shadow.discrepancy import discrepancy_rate; print(discrepancy_rate(window='1h'))"

# 3. checksum 一致率 >= 99.9% (>= 999/1000 windows green)
python -c "from parallax.shadow.discrepancy import checksum_consistency; print(checksum_consistency(window='1h'))"
```

## Rollback Procedure

The shadow path is gated by a single environment variable. Recovery is **flag-flip only — no redeploy**.

```bash
# On the ZenBook (and any other Parallax instance running shadow):
sudo sed -i 's/^SHADOW_MODE=true/SHADOW_MODE=false/' /path/to/parallax.env
sudo systemctl restart parallax

# Or, if hot-flipping in a dev shell that already has the process running:
export SHADOW_MODE=false
# The next request reads os.environ.get("SHADOW_MODE", "false") inside
# _is_enabled() and bypasses the shadow path. No process restart needed,
# only an env mutation visible to the running interpreter.
```

Belt-and-braces: emptying `SHADOW_USER_ALLOWLIST` short-circuits even if `SHADOW_MODE=true`:

```bash
export SHADOW_USER_ALLOWLIST=
```

Recovery target: **< 5 minutes** from incident detection (Grafana alert / Chris noticing) to the next request bypassing shadow.

The hard invariants in `parallax/router/shadow.py` mean a shadow-side bug cannot corrupt user-facing data — `query()` always returns the canonical result; shadow exceptions are caught and logged as `arbitration_outcome=shadow_only` without propagating. So a flag flip is sufficient even in worst-case scenarios.

## Files Touched (for PR descriptions / change-log)

### `feat/m2-wal` (Iter 1 + Iter 2)
- `parallax/wal.py` (new, then modified) — `WALQueue` SQLite-backed offline queue, stdlib-only; eviction telemetry via `logging.getLogger("parallax.wal")`
- `tests/test_wal.py` (new, then extended) — 14 unit tests + 2 caplog tests for eviction telemetry
- `tests/integration/test_wal_offline_replay.py` (new) — M2 DoD integration test (5 enqueues during outage + reconnect → zero loss)
- `plugins/parallax-session-hook/hook.py` (modified) — inlined `_WALQueue` (stdlib-only client copy) + `_drain_wal` on hook start + eviction `_log_debug`

### `feat/lane-c-us-006-shadow-mode` (Iter 1 + Iter 2)
- `parallax/router/shadow.py` (new, then modified) — `ShadowInterceptor` + 9-field `ShadowDecisionLog` + `math.isclose` score compare + UTC daily rotation + cached log dir
- `tests/router/test_shadow_interceptor.py` (new, then extended) — 17 tests covering schema (9 fields) / bypass / divergence / latency / correlation_id / FP-drift score tolerance / UTC rotation / one-time mkdir
