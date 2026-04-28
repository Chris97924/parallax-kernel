# M3 — L2 Dual-Read Stable Rollout Runbook

**Scope:** M3a US-011 — dual-read mechanism (L2 crosswalk + Aphelion shadow reads, flag-gated).
**Date authored:** 2026-04-28.
**Ralplan:** `E:\Parallax\.omc\plans\ralplan-m3-l2-dualread-2026-04-27.md` §3 M3-T1.3, §10 Q10/Q11.
**Audience:** Operator deploying M3a to ZenBook production.

---

## Branches & Commits

| Task | Scope |
|---|---|
| T0 | crosswalk schema + migration |
| T1.1 | AphelionReadAdapter stub + config |
| T1.2 | dual-read orchestrator |
| T1.3 | rollout runbook + Grafana + Prometheus rules (this doc) |
| T1.4 | synthetic load + DoD assertion scripts |
| T1.5 | circuit breaker + drain wiring |

All T0–T1.5 branches must be merged to `main-next` before starting the 48h SLO window.

---

## Pre-flight Checklist

**All must be true before starting the 48h SLO observation window.**

- [ ] M2 72h DoD window confirmed GREEN:
  - `parallax_shadow_observation_diverge_rate < 0.3%`
  - `parallax_shadow_checksum_consistency >= 99.9%`
  - `parallax_shadow_log_records_total > 0` (non-zero — writer running)
- [ ] All M3a PRs (T0/T1.1/T1.2/T1.3/T1.4/T1.5) squash-merged to `main-next`.
- [ ] `crosswalk` table backfilled. Run:

  ```bash
  cd /path/to/parallax
  python -c "
import os, sqlite3
from parallax.router.crosswalk_backfill import backfill_crosswalk
conn = sqlite3.connect(os.environ['PARALLAX_DB_PATH'])
print(backfill_crosswalk(conn, user_id='chris'))
conn.close()
"
  ```

  Verify with:

  ```bash
  python -c "
  import sqlite3, os
  db = os.environ.get('PARALLAX_DB_PATH', 'parallax.db')
  conn = sqlite3.connect(db)
  count = conn.execute('SELECT COUNT(*) FROM crosswalk').fetchone()[0]
  print(f'crosswalk rows: {count}')
  conn.close()
  "
  ```

  Expected: > 0 rows; 0 means backfill failed or no content yet.

- [ ] Grafana dashboard imported (`grafana/dashboards/parallax-dual-read-observability.json`).

  ```bash
  curl -X POST -H "Content-Type: application/json" \
    -H "Authorization: Bearer $GRAFANA_TOKEN" \
    http://grafana.internal:3000/api/dashboards/db \
    --data-binary @grafana/dashboards/parallax-dual-read-observability.json
  ```

- [ ] Prometheus alert rules loaded (`prometheus/rules/parallax-dual-read.rules.yml`).

  ```bash
  # Validate syntax first (if promtool available):
  promtool check rules prometheus/rules/parallax-dual-read.rules.yml

  # Reload Prometheus config after copying rules file to Prometheus rules dir:
  curl -X POST http://prometheus.internal:9090/-/reload
  ```

- [ ] `DUAL_READ` env var confirmed `false` on all instances:

  ```bash
  # ZenBook:
  grep DUAL_READ /path/to/parallax.env
  # Expected: DUAL_READ=false  (or key absent, defaults false)
  ```

- [ ] AphelionReadAdapter is a **stub only** — confirm no real HTTP calls in M3a:

  ```bash
  grep -r "AphelionReadAdapter" parallax/ | grep -v "stub\|test\|# "
  # Must show only stub implementation — no live HTTP client wired
  ```

  **Critical:** M3a MUST NOT make real Aphelion calls. Real HTTP client ships in M3b/M4.

- [ ] Service restarted and `/metrics` endpoint responding:

  ```bash
  curl -s http://127.0.0.1:8765/metrics | grep parallax_dual_read
  ```

---

## 48h SLO Observation Window (§Q11 — Automated Gate)

**State during window:** `DUAL_READ=false`. Backfill running. Lazy materialization paths exercised by synthetic load (T1.4 scripts).

**Goal:** verify `parallax_crosswalk_miss_rate < 5%` at h+48 before any flag flip.

### Starting the window

Record the UTC timestamp when you begin:

```bash
echo "48h SLO window started: $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee /var/log/parallax/m3-slo-window.log
```

### h+24 checkpoint (manual)

```bash
# Scrape current crosswalk miss rate from /metrics:
curl -s http://127.0.0.1:8765/metrics | grep parallax_crosswalk_miss

# Expected: parallax_crosswalk_miss_orphan_total < 5% of parallax_dual_read_outcomes_total
# Record reading in /var/log/parallax/m3-slo-window.log
echo "h+24 crosswalk_miss_orphan_total: <VALUE> / dual_read_outcomes_total: <VALUE>" \
  >> /var/log/parallax/m3-slo-window.log
```

### h+48 checkpoint (final gate)

```bash
curl -s http://127.0.0.1:8765/metrics | grep -E "parallax_crosswalk_miss|parallax_dual_read_outcomes"
echo "h+48 crosswalk_miss_orphan_total: <VALUE> / dual_read_outcomes_total: <VALUE>" \
  >> /var/log/parallax/m3-slo-window.log
```

Gate: `(miss_orphan_total / outcomes_total) < 0.05`. If passes, proceed to Flag Flip Procedure.

### Alert rule — automated detection

The `CrosswalkMissRateHigh` alert fires when:
```
rate(parallax_crosswalk_miss_orphan_total[5m]) / rate(parallax_dual_read_outcomes_total[5m]) > 0.05
```
sustained for **30 minutes**. If this alert fires during the 48h window, see **Abort criteria** below.

### Abort criteria

If `CrosswalkMissRateHigh` fires **or** the h+48 reading exceeds 5%:

1. Re-run backfill:

   ```bash
   python -c "
import os, sqlite3
from parallax.router.crosswalk_backfill import backfill_crosswalk
conn = sqlite3.connect(os.environ['PARALLAX_DB_PATH'])
print(backfill_crosswalk(conn, user_id='chris'))
conn.close()
"
   ```

2. Restart the 48h window timer from zero.
3. Log the abort reason.

---

## Flag Flip Procedure (Post-48h-SLO Clearance)

### Step 1 — Canary flip (single-user deployment)

Parallax is single-user (chris-only) per Q14 — there is no per-user
allowlist. The "canary" is the full 24h observation under `DUAL_READ=true`
on the chris-only deployment. If/when multi-user federation lands, a
`DUAL_READ_ALLOWLIST` env var will be added; for M3a it is intentionally
absent (YAGNI). Future-multi-user prep is tracked separately.

```bash
# Edit ZenBook EnvironmentFile:
DUAL_READ=true

sudo systemctl daemon-reload
sudo systemctl restart parallax
```

Record canary start time:

```bash
echo "Canary start (DUAL_READ=true, single-user chris): $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >> /var/log/parallax/m3-slo-window.log
```

### Step 2 — 24h canary metrics check

At h+24 post-canary-flip, verify all 5 DoD metrics are green:

```bash
curl -s http://127.0.0.1:8765/metrics | grep -E \
  "parallax_dual_read_discrepancy_rate|\
parallax_aphelion_unreachable_rate|\
parallax_circuit_breaker_tripped_total|\
parallax_inflight_requests|\
parallax_drain_timeout_total"
```

| Metric | DoD threshold | Status |
|---|---|---|
| `parallax_dual_read_discrepancy_rate` | < 0.001 (0.1%) | [ ] |
| `parallax_aphelion_unreachable_rate` | < 0.005 (0.5%) | [ ] |
| `parallax_circuit_breaker_tripped_total` | == 0 (no trips) | [ ] |
| `parallax_inflight_requests` | healthy (no stuck pile-up) | [ ] |
| `parallax_drain_timeout_total` | == 0 | [ ] |

If all green, proceed to Step 3. If any red, see **Rollback Procedure**.

### Step 3 — Promote canary to 72h DoD window

After 24h canary green, the same `DUAL_READ=true` setting becomes the
production state — no env-var change needed because there is no allowlist
to widen on a single-user deployment. Just record the transition.

```bash
# Sanity-check the env is still set:
sudo systemctl show parallax --property=Environment | grep DUAL_READ

# Service restart is NOT required at this step — flag is already live.
```

```bash
echo "Full flip (DUAL_READ=true, all users): $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  >> /var/log/parallax/m3-slo-window.log
echo "72h DoD window started." >> /var/log/parallax/m3-slo-window.log
```

Monitor all 5 DoD metrics continuously via Grafana `parallax-dual-read-observability` dashboard throughout the 72h window.

---

## Rollback Procedure

Trigger: any DoD metric breaches (see thresholds above) or any `severity: critical` alert fires.

### Step 1 — Disable DUAL_READ

```bash
# Edit EnvironmentFile:
DUAL_READ=false

sudo systemctl daemon-reload
# Send SIGTERM — lifespan handler drains in-flight (up to 15 min):
sudo systemctl reload parallax
# If reload doesn't trigger graceful stop, use restart:
# sudo systemctl restart parallax
```

**PM2 variant:**

```bash
# Set env and graceful reload:
pm2 env set parallax DUAL_READ false
pm2 reload parallax --update-env
```

The lifespan handler `parallax_lifespan` (in `parallax/server/lifespan.py`) drains in-flight requests up to `DRAIN_TIMEOUT_SECONDS=900` (15 minutes). If drain times out, `parallax_drain_timeout_total` increments — see Failure-Modes §Drain timeout.

### Step 2 — Verify rollback

```bash
# Wait ~30s for requests to drain, then check:
curl -s http://127.0.0.1:8765/metrics | grep -E \
  "parallax_inflight_requests|\
parallax_dual_read_discrepancy_rate|\
parallax_aphelion_unreachable_rate"
```

Expected post-rollback:
- `parallax_inflight_requests` → 0 (or stabilized at pre-rollout baseline)
- `parallax_dual_read_discrepancy_rate` → 0 (no dual-read traffic)
- `parallax_aphelion_unreachable_rate` → 0

### Step 3 — Re-roll-forward criteria

Before attempting another flag flip:

1. Root cause identified and fix merged to `main-next`.
2. 24h canary observation (with fix) shows clean metrics.
3. Re-run the full 72h DoD window from scratch.

---

## Failure-Modes

### Aphelion unreachable spike (Q10)

**Detection:**
- `parallax_aphelion_unreachable_rate > 1%` sustained 5 min → `AphelionUnreachableRateHigh` alert fires.
- Circuit breaker trips → new dual-read requests get `request.state.dual_read = false` automatically.
- `parallax_circuit_breaker_tripped_total` increments per trip event → `CircuitBreakerTripped` alert fires immediately.

**In-flight cohort behavior:**
- Requests already mid-flight keep their pre-trip dual-read snapshot until natural completion.
- Shadow timeout bound: 100ms (see `parallax/router/shadow.py:172-182`).

**Manual re-arm (after Aphelion recovers):**

```bash
# Verify Aphelion is healthy first:
curl -s http://aphelion.internal/health

# Then reset the circuit breaker:
curl -X POST -H "Authorization: Bearer $PARALLAX_ADMIN_TOKEN" \
  http://127.0.0.1:8765/admin/circuit_breaker/reset
```

Only re-arm after confirming `parallax_aphelion_unreachable_rate` has dropped below 0.5% for at least 5 minutes.

### Drain timeout (Q8)

**Detection:** `parallax_drain_timeout_total > 0` → `DrainTimeoutDetected` alert fires.

**Cause:** In-flight requests refused to complete within 15 minutes — likely a stuck upstream connection or blocking query.

**Mitigation:**

```bash
# Identify stuck requests in application logs:
sudo journalctl -u parallax --since="15 minutes ago" | grep "in_flight\|stuck\|timeout"

# If a specific request is identified as stuck, force-kill (accepts 502s for that request):
sudo systemctl kill parallax  # last resort — graceful SIGTERM already timed out
```

Note: force-kill causes in-flight requests to 502, but the rollback completes. Investigate the root cause before re-enabling DUAL_READ.

### Crosswalk miss spike (Q11)

**Detection:**
- `parallax_crosswalk_miss_orphan_total` rate climbing.
- `CrosswalkMissRateHigh` alert fires when `crosswalk_miss_rate > 5%` for 30 min.

**Cause:** Ingest pipeline added content faster than crosswalk backfill caught up, OR a content_hash collision (rare).

**Mitigation:**

```bash
# Re-run backfill with a fresh batch:
python -c "
import os, sqlite3
from parallax.router.crosswalk_backfill import backfill_crosswalk
conn = sqlite3.connect(os.environ['PARALLAX_DB_PATH'])
print(backfill_crosswalk(conn, user_id='chris'))
conn.close()
"

# If miss rate stays high after backfill, inspect ingest pipeline:
curl -s http://127.0.0.1:8765/metrics | grep parallax_ingest
```

If persistent (> 2 re-runs), pause ingest pipeline temporarily, complete backfill, then re-enable ingest. Log the incident.

---

## Operator Checklist Appendix

Copy-paste for ops log. Mark `[x]` as each step completes.

### Pre-flight

- [ ] M2 72h DoD confirmed GREEN (all 3 metrics)
- [ ] All M3a PRs (T0/T1.1/T1.2/T1.3/T1.4/T1.5) merged to `main-next`
- [ ] `crosswalk` table backfilled (row count > 0)
- [ ] Grafana `parallax-dual-read-observability` dashboard imported
- [ ] Prometheus `parallax-dual-read.rules.yml` loaded and rules validated
- [ ] `DUAL_READ=false` confirmed on ZenBook env
- [ ] `DUAL_READ=false` confirmed on local dev env
- [ ] `DUAL_READ=false` confirmed in CI
- [ ] AphelionReadAdapter confirmed as stub (no live HTTP calls)
- [ ] Service restarted; `/metrics` responding with `parallax_dual_read_*` metrics
- [ ] Grafana panels all showing data (not "No data")

### 48h SLO observation window

- [ ] Window start time recorded in `/var/log/parallax/m3-slo-window.log`
- [ ] h+24: crosswalk miss rate reading recorded (value: ___)
- [ ] h+24: no `CrosswalkMissRateHigh` alert fired
- [ ] h+48: crosswalk miss rate reading recorded (value: ___)
- [ ] h+48: miss rate < 5% confirmed → SLO PASSED
- [ ] (If abort) Backfill re-run; window timer reset

### Canary flag flip (Step 1)

- [ ] `DUAL_READ=true` set in EnvironmentFile (single-user deployment, no allowlist)
- [ ] Service daemon-reload + restart completed
- [ ] Canary start time recorded
- [ ] First dual-read request verified in metrics (discrepancy_rate metric non-null)

### 24h canary check (Step 2)

- [ ] `parallax_dual_read_discrepancy_rate` < 0.1%
- [ ] `parallax_aphelion_unreachable_rate` < 0.5%
- [ ] `parallax_circuit_breaker_tripped_total` == 0
- [ ] `parallax_inflight_requests` healthy
- [ ] `parallax_drain_timeout_total` == 0
- [ ] No critical alerts fired during canary window

### Full flip (Step 3)

- [ ] `DUAL_READ=true` confirmed live (no env change needed for single-user)
- [ ] 72h DoD window start time recorded
- [ ] Grafana dashboard pinned to 72h window

### Rollback (if triggered)

- [ ] Rollback decision time and triggering metric recorded
- [ ] `DUAL_READ=false` set in EnvironmentFile
- [ ] Service reload/restart issued (SIGTERM → graceful drain)
- [ ] `parallax_inflight_requests` → 0 confirmed
- [ ] `parallax_dual_read_discrepancy_rate` → 0 confirmed
- [ ] `parallax_aphelion_unreachable_rate` → 0 confirmed
- [ ] Root cause investigation opened
- [ ] Fix merged before re-roll-forward attempt
