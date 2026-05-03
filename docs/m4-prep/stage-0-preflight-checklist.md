```markdown
# M4 Stage 0 — Pre-flight Checklist

> **文件版本**: v1.0.0  
> **最後更新**: 2025-01-15  
> **Owner**: Parallax SRE  
> **審核者**: Chris (拍板者)  
> **狀態**: ACTIVE  

---

## 1. 文件用途

### 受眾

| 角色 | 職責 |
|------|------|
| M4 canary deployment oncall 工程師 | 逐項勾選、執行驗證命令、回報結果 |
| Chris (拍板者) | Final GO/NO-GO 簽核 |

### 用途

Stage @1% 啟動前 **T-1 hour** 內，oncall 工程師依本文件逐項勾選。  
**全綠 → GO**，啟動 stage @1%。  
**任一紅 → STOP**，記錄 blocker 於 `m4-launch-blockers.md`，通知 Chris。

### 與上游文件之關係

```
us-009-acceptance-criteria.md   ← 定義 US-009.1 / US-009.2 / US-009.3 acceptance criteria
         │
         ▼
canary-stage-runbook.md         ← 4-stage 推進 SOP (1% → 10% → 50% → 100%)
         │
         ▼
stage-0-preflight-checklist.md  ← 本文件：啟動前 gate check (全綠才 GO)
```

---

## 2. M3 14-day Corpus DoD Verification（必要前置）

> M3 corpus 穩定運行 14 天後，以下指標必須連續 72 小時達標。  
> 任一項未達標 → **STOP**，不得啟動 M4 canary。

### Checklist

- [ ] **dual_read_discrepancy_rate < 0.1%** 連續 72h
- [ ] **arbitration_conflict_rate < 1%** 連續 72h
- [ ] **dual_read_write_error_rate < 0.02%** 連續 72h
- [ ] **aphelion_unreachable_rate < 0.5%**（待 PR #27 / US-006 deploy 後可驗）
- [ ] **crosswalk_miss_rate < 5%**（測量窗口 +48h）
- [ ] **circuit_open_count_72h < 3**

### 驗證命令

```bash
# dual_read_discrepancy_rate — 連續 72h < 0.1%
dual_read_continuity_check \
  --since=72h \
  --metric=discrepancy \
  --format=json
# 預期 exit 0，JSON 內 "pass": true

# arbitration_conflict_rate — 連續 72h < 1%
dual_read_continuity_check \
  --since=72h \
  --metric=arbitration_conflict \
  --format=json
# 預期 exit 0

# dual_read_write_error_rate — 連續 72h < 0.02%
dual_read_continuity_check \
  --since=72h \
  --metric=write_error \
  --format=json
# 預期 exit 0

# aphelion_unreachable_rate — < 0.5% (PR #27 deploy 後)
curl -s "http://prometheus:9090/api/v1/query" \
  --data-urlencode 'query=rate(aphelion_unreachable_total[72h]) / rate(aphelion_requests_total[72h])' \
  | jq '.data.result[0].value[1]'
# 預期 < 0.005

# crosswalk_miss_rate — < 5% (測量窗口 +48h)
curl -s "http://prometheus:9090/api/v1/query" \
  --data-urlencode 'query=rate(crosswalk_miss_total[48h]) / rate(crosswalk_requests_total[48h])' \
  | jq '.data.result[0].value[1]'
# 預期 < 0.05

# circuit_open_count_72h — < 3
curl -s "http://prometheus:9090/api/v1/query" \
  --data-urlencode 'query=increase(circuit_open_total[72h])' \
  | jq '.data.result[0].value[1]'
# 預期 < 3
```

---

## 3. Aphelion v0.5.x Package Toolkit Verification

> ⚠️ **重要**：依據 xcouncil verdict，M4 僅使用 Aphelion **package format**，  
> **NOT retrieval API**。真實 HTTP adapter 延至 M5+ ticket。

### Checklist

- [ ] **Aphelion v0.5.0+ 已部署**（package format only，非 retrieval API）
- [ ] **aphelion_stub.py:53** 回傳 `status='secondary_unavailable'`（US-009.2 null-stub）
- [ ] **無真實 HTTP adapter wired**（deferred to M5+ ticket）

### 驗證命令

```bash
# 確認 Aphelion package 版本 >= 0.5.0
pip show aphelion 2>/dev/null | grep -i version
# 或
python -c "import aphelion; print(aphelion.__version__)"
# 預期 >= 0.5.0

# 確認 stub 行為：fetch 回傳 secondary_unavailable
grep -A2 'def fetch' parallax/router/aphelion_stub.py | head -5
# 預期看到 status='secondary_unavailable'

# 確認無真實 HTTP adapter 被 import
grep -rn 'import.*aphelion.*http\|from.*aphelion.*http' parallax/ --include='*.py'
# 預期：無輸出（exit 0, empty result）

# 確認 stub 是唯一 wired 的 aphelion backend
grep -rn 'aphelion_stub\|AphelionStub' parallax/router/ --include='*.py'
# 預期：僅出現在 router config 中
```

---

## 4. Canary Infra (US-009.1) Ready Check

> US-009.1 定義 canary 基礎設施：idempotency、audit log、auto-rollback triggers。

### Checklist

- [ ] **event_id 使用 UUID v7**（NOT hash-with-timestamp — clock drift 有 dup 風險）
- [ ] **audit_log SQLite table 已建立**（schema: `event_id` PK, `request_at_iso`, `response_status`, ...）
- [ ] **5 個 auto-rollback triggers 已接線**：
  - error rate ≥ 0.5% / 5min window
  - discrepancy rate ≥ 0.5% / 3min window
  - p99 latency ≥ 100ms / 5min window
  - data_loss > 0 → **immediate** rollback
  - minimum 50 hits 保護（避免小樣本誤觸發）
- [ ] **hysteresis 30min cooldown** + manual ACK re-promote logic 已實作

### 驗證命令

```bash
# 一鍵檢查 canary infra readiness
parallax canary --check-infra --pretend
# 預期 exit 0，所有 sub-check PASS

# 確認 event_id 為 UUID v7
grep -n 'uuid7\|uuid_v7\|UUIDv7' parallax/canary/event_id.py
# 預期：有對應 import / function call

# 確認 audit_log schema
sqlite3 parallax_canary.db ".schema audit_log"
# 預期欄位：event_id TEXT PRIMARY KEY, request_at_iso TEXT, response_status INTEGER, ...

# 確認 5 個 rollback triggers
parallax canary --list-triggers
# 預期輸出 5 個 trigger，含對應 threshold + window

# 確認 hysteresis cooldown 設定
grep -A5 'cooldown\|hysteresis' parallax/canary/rollback.py
# 預期：cooldown_seconds=1800 (30min)
```

---

## 5. DoD Scripts (US-009.3) Ready Check

> US-009.3 定義 rollback drill 與 drain 行為驗證。

### Checklist

- [ ] **rollback drill harness 就緒**：`parallax canary --rollback-drill --dry-run` exit 0
- [ ] **drain in-flight requests**（NO replay）per design
- [ ] **Orbit re-emit + idempotency 保護**驗過至少 1 次（drain test）

### 驗證命令

```bash
# rollback drill — dry run
parallax canary --rollback-drill --dry-run
# 預期 exit 0，輸出 drill steps + simulated metrics

# drain in-flight requests 驗證
parallax canary --drain-test --timeout=60s
# 預期：所有 in-flight requests drain 完畢，無 replay

# Orbit re-emit + idempotency 保護驗證
parallax canary --orbit-reemit-test
# 預期：re-emit 後 event_id dedup 正常，無重複寫入
# 檢查 audit_log 中同一 event_id 僅出現一次
sqlite3 parallax_canary.db \
  "SELECT event_id, COUNT(*) as cnt FROM audit_log GROUP BY event_id HAVING cnt > 1"
# 預期：無輸出（無重複）
```

---

## 6. Observability Ready Check

> 確保監控、告警、通知管道在 canary 啟動前全部到位。

### Checklist

- [ ] **Grafana panel `m4-canary-stage-1`** 已加入 dashboard
- [ ] **Prometheus alert rule `m4-canary-rules.yml`** 已部署（5 個 triggers）
- [ ] **PagerDuty / Slack webhook** 已設定（P0/P1 alerts）
- [ ] **runbook (`canary-stage-runbook.md`)** 已分享給 oncall 團隊

### 驗證命令

```bash
# 確認 Grafana dashboard 存在
curl -s -H "Authorization: Bearer ${GRAFANA_TOKEN}" \
  "http://grafana:3000/api/search?query=m4-canary-stage-1" \
  | jq '.[].title'
# 預期：包含 "m4-canary-stage-1"

# 確認 Prometheus alert rules 已載入
curl -s "http://prometheus:9090/api/v1/rules" \
  | jq '.data.groups[] | select(.name | contains("m4-canary")) | .rules | length'
# 預期：>= 5

# 確認 alert rule 檔案存在
ls -la /etc/prometheus/rules/m4-canary-rules.yml
# 預期：檔案存在且非空

# 確認 PagerDuty / Slack webhook
parallax canary --check-alerting
# 預期：PagerDuty + Slack 均回 200 OK

# 確認 runbook 已分享
ls -la docs/m4-prep/canary-stage-runbook.md
# 預期：檔案存在
```

---

## 7. Rollback Path Drill（T-1h 內驗 1 次）

> **T-1 hour** 內必須實際走過一次完整 rollback 路徑，確認 < 30 min 完成。

### Rollback 步驟

| 步驟 | 動作 | 預期耗時 |
|------|------|----------|
| 1 | drain in-flight requests（60s wait） | ~60s |
| 2 | flag flip `CANARY_ENABLED=false` | ~5s |
| 3 | Orbit re-emit（idempotency 保護） | ~120s |
| 4 | verify metric 回 baseline（5 min check） | ~300s |

### Checklist

- [ ] **T-1h 內走過上述 4 步**，confirm 總時間 < 30 min

### 驗證命令

```bash
# 步驟 1: drain in-flight requests
parallax canary --drain --timeout=60s
echo "Step 1 done — drain complete"

# 步驟 2: flag flip
parallax canary --set CANARY_ENABLED=false
echo "Step 2 done — canary disabled"

# 步驟 3: Orbit re-emit
parallax orbit --re-emit --idempotent
echo "Step 3 done — Orbit re-emit complete"

# 步驟 4: verify metric 回 baseline
sleep 300
parallax canary --verify-baseline --window=5m
echo "Step 4 done — baseline verified"

# 總時間驗證
echo "Rollback drill complete — verify total time < 30 min"
```

---

## 8. Stakeholder Communication

> 確保所有相關人員已通知、排班已確認。

### Checklist

- [ ] **Chris 拍板**（signed off in Notion P×A dashboard）
- [ ] **oncall 排班確認**（24h 後 stage @10% 推進，oncall 在線）
- [ ] **Slack `#parallax-deploy` 公告**（T-30min）
- [ ] **Status page 更新**（canary in progress）

### 驗證命令

```bash
# 確認 Notion sign-off (手動檢查)
echo "→ 請至 Notion P×A dashboard 確認 Chris 已 sign off M4 stage @1%"
echo "   URL: https://notion.so/parallax/pxa-dashboard"

# 確認 oncall 排班
parallax oncall --check-schedule --next=24h
# 預期：oncall 工程師已排班且在線

# Slack 公告 (T-30min)
curl -X POST "${SLACK_WEBHOOK_URL}" \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "🚀 M4 Canary Stage @1% 啟動中 — T-30min\nOncall: @engineian\nRunbook: <https://wiki.parallax/canary-stage-runbook|canary-stage-runbook.md>"
  }'
# 預期：200 OK

# Status page 更新
parallax status-page --update \
  --message="M4 canary deployment in progress (stage @1%)" \
  --status=investigating
# 預期：更新成功
```

---

## 9. Final GO / NO-GO

### 判定規則

| 狀態 | 條件 | 動作 |
|------|------|------|
| ✅ **GO** | 第 2 ~ 8 章節所有 checkbox 全綠 | 執行 stage @1% 啟動命令 |
| 🛑 **NO-GO** | 任一 checkbox 為紅 | **STOP**，記錄 blocker 於 `m4-launch-blockers.md`，通知 Chris |

### NO-GO 流程

```bash
# 建立 blocker 記錄
cat >> docs/m4-prep/m4-launch-blockers.md << 'EOF'

## Blocker — $(date -u +%Y-%m-%dT%H:%M:%SZ)

- **Section**: [填入章節編號]
- **Checkbox**: [填入未通過項目]
- **Root cause**: [填入原因]
- **Owner**: [填入負責人]
- **ETA to fix**: [填入預計修復時間]
EOF

# 通知 Chris
echo "🛑 M4 Stage @1% NO-GO — blocker 已記錄，請見 m4-launch-blockers.md"
```

### GO 流程

全 8 章節 checkbox 全綠後，執行 stage @1% 啟動命令（見 §10）。

---

## 10. 範例命令（Bash Code Blocks 完整版）

```bash
# ============================================
# 啟動 stage @1%
# ============================================
parallax canary --start --stage=1
# 預期：canary 啟動，1% 流量導入

# ============================================
# 即時查看 canary status + dashboard URL
# ============================================
parallax canary --status
# 預期：輸出 current stage, metrics summary, Grafana dashboard URL

# ============================================
# 緧急 rollback
# ============================================
parallax canary --abort
# 預期：立即停止 canary，drain in-flight，flag flip，Orbit re-emit

# ============================================
# 查看當前 stage 推進歷史
# ============================================
parallax canary --history
# 預期：列出所有 stage transition + timestamp + metrics snapshot

# ============================================
# 手動推進至下一 stage (需 Chris ACK)
# ============================================
parallax canary --promote --stage=10 --ack-by=chris
# 預期：推進至 stage @10%，需 Chris 簽核
```

---

## Appendix: Checklist Summary Table

| # | 章節 | Checkbox 數 | 狀態 |
|---|------|-------------|------|
| 2 | M3 14-day Corpus DoD | 6 | ☐ |
| 3 | Aphelion v0.5.x Toolkit | 3 | ☐ |
| 4 | Canary Infra (US-009.1) | 4 | ☐ |
| 5 | DoD Scripts (US-009.3) | 3 | ☐ |
| 6 | Observability | 4 | ☐ |
| 7 | Rollback Path Drill | 1 | ☐ |
| 8 | Stakeholder Communication | 4 | ☐ |
| **Total** | | **25** | |

> **全 25 項 checkbox 全綠 → GO**  
> **任一紅 → STOP → 記錄 blocker → 通知 Chris**
```
