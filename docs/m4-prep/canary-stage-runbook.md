# M4 L3 Canary — 4-Stage Runbook

> **文件版本**: v1.0  
> **最後更新**: 2025-07-11  
> **擁有者**: Parallax SRE  
> **狀態**: Draft → Review by Chris

---

## 1. 文件目的與受眾

本文件為 **M4 L3 Canary 100% 全量推進** 的操作手冊，涵蓋四個階段（1% → 10% → 50% → 100%）的逐步執行指南。

**受眾**：
- **Parallax oncall 工程師**：主要執行者，依本文件逐步推進 stage、驗證 DoD、執行 rollback。
- **Chris**：stage 推進的最終審批人（Go/No-Go decision maker）。

**使用方式**：每個 stage 啟動前，oncall 需完整閱讀對應章節；推進前必須取得 Chris 的書面 ACK（Slack #m4-canary 頻道）。

---

## 2. 前置條件（Stage @1% 啟動前）

以下所有項目必須在首次推進前完成驗證：

- [ ] M4 binary 已 build 並推送至 canary image registry，tag 格式為 `m4-canary-<git-sha>`。
- [ ] Canary feature flag (`ENABLE_M4_CANARY`) 已在 config store 建立，預設值為 `false`。
- [ ] Canary traffic tag (`canary-l3-m4`) 已在 load balancer 規則中定義完畢。
- [ ] Monitor dashboard 已建立：`https://grafana.internal/d/m4-canary-overview`。
- [ ] Alert rules 已匯入 PagerDuty，包含：
  - `m4_write_success_rate < 99.9%`（Critical）
  - `m4_latency_p99_delta > 10%`（Warning）
  - `m4_data_loss_count > 0`（Critical）
  - `m4_conflict_count_new > 0`（Warning）
- [ ] Rollback playbook 已於 staging 環境完成 dry-run 演練。
- [ ] Orbit re-emit endpoint 已驗證 idempotency（重複呼叫不產生副作用）。
- [ ] Chris 已簽核 stage @1% Go/No-Go。

---

## 3. Stage @1%（觀察期 24 小時）

### 3.1 啟動步驟

1. SSH 進入 canary control plane：
   ```bash
   ssh canary-ctl.parallax.internal
   ```
2. 設定環境變數並啟動：
   ```bash
   export CANARY_TAG=canary-l3-m4
   export CANARY_WEIGHT=1
   export FEATURE_FLAG=ENABLE_M4_CANARY
   ./canary-deploy.sh --stage 1pct --confirm
   ```
3. 確認 dashboard 上 `canary-l3-m4` tag 的流量佔比顯示為 **~1%**。
4. 於 Slack `#m4-canary` 發布啟動通知：
   > `🚀 M4 Canary Stage @1% 已啟動。觀察期 24h，回滾 SLA 30 min。oncall: <你的名字>`

### 3.2 DoD 驗證（每 6 小時執行）

每 6 小時（T+0h, T+6h, T+12h, T+18h, T+24h）執行以下檢查：

| 指標 | 閾值 | 查詢方式 |
|------|------|----------|
| 寫入成功率 | ≥ 99.9% | Dashboard panel `Write Success Rate (Canary)` |
| 延遲增幅 | ≤ 10% vs baseline | Dashboard panel `P99 Latency Delta` |

若任一指標 breach，**立即進入 rollback 流程**（見第 7 節）。

### 3.3 通過判定 → 推進 @10%

- 24h 內所有 DoD 指標持續通過。
- 無 Critical alert 觸發。
- 取得 Chris 於 Slack 的 ACK：`Stage @1% PASS → proceed to @10%`。

### 3.4 失敗 → Rollback（30 分鐘內完成）

執行第 7 節 Rollback Playbook，目標 **30 分鐘內** 完成全部步驟。完成後通知 Chris 並記錄失敗原因。

---

## 4. Stage @10%（觀察期 48 小時）

### 4.1 啟動步驟

1. 更新 canary weight：
   ```bash
   export CANARY_WEIGHT=10
   ./canary-deploy.sh --stage 10pct --confirm
   ```
2. 確認 dashboard 顯示流量佔比 **~10%**。
3. Slack 通知：
   > `🚀 M4 Canary Stage @10% 已啟動。觀察期 48h，回滾 SLA 1-2h。oncall: <你的名字>`

### 4.2 DoD 驗證（每 12 小時執行）

| 指標 | 閾值 |
|------|------|
| Data loss | = 0（零容忍） |
| 新增 conflict | 無新增（與 @1% baseline 比對） |
| 寫入成功率 | ≥ 99.9%（持續監控） |
| 延遲增幅 | ≤ 10%（持續監控） |

### 4.3 通過判定 → 推進 @50%

- 48h 內 data_loss = 0 且無新增 conflict。
- 所有持續監控指標穩定。
- Chris ACK。

### 4.4 失敗 → Rollback（1-2 小時內完成）

執行 Rollback Playbook。此階段回滾 SLA 較寬裕（1-2h），但仍需盡快完成。回滾後需額外執行 data integrity scan。

---

## 5. Stage @50%（觀察期 72 小時）

### 5.1 啟動步驟

1. 更新 canary weight：
   ```bash
   export CANARY_WEIGHT=50
   ./canary-deploy.sh --stage 50pct --confirm
   ```
2. 確認 dashboard 顯示流量佔比 **~50%**。
3. Slack 通知：
   > `🚀 M4 Canary Stage @50% 已啟動。觀察期 72h，回滾 SLA 2-4h。oncall: <你的名字>`

### 5.2 DoD 驗證（每 12 小時執行）

所有 @10% 指標持續維持，額外關注：

- **資源用量**：CPU / memory / disk I/O 是否在預期範圍內。
- **下游依賴**：L2 / L1 層是否有異常延遲或 error spike。

### 5.3 通過判定 → 推進 @100%

- 72h 全量指標穩定。
- 無任何 Critical / Warning alert。
- Chris ACK。

### 5.4 失敗 → Rollback（2-4 小時內完成）

執行 Rollback Playbook。50% 流量回滾需更謹慎，建議 drain 時間拉長至 10 分鐘。

---

## 6. Stage @100%（觀察期 1 週）

### 6.1 啟動步驟

1. 更新 canary weight：
   ```bash
   export CANARY_WEIGHT=100
   ./canary-deploy.sh --stage 100pct --confirm
   ```
2. 確認 dashboard 顯示流量佔比 **100%**。
3. Slack 通知：
   > `🚀 M4 Canary Stage @100% 已啟動。觀察期 1 週，回滾 SLA 4h+ full playbook。oncall: <你的名字>`

### 6.2 DoD 驗證（每 24 小時執行）

- 所有先前階段指標持續維持。
- **全量穩定性**：無 performance regression、無 capacity 瓶頸。
- **Rollback playbook 演練**：於 T+48h 前完成一次 staging 環境的完整 rollback 演練，記錄結果。

### 6.3 通過判定 → M4 GA

- 1 週觀察期內所有指標通過。
- Rollback playbook 演練成功。
- Chris 最終簽核：`M4 L3 Canary @100% PASS → GA`。

### 6.4 失敗 → Rollback（4 小時+ full playbook）

執行完整 Rollback Playbook。此階段涉及全量流量，回滾時間較長，需嚴格按照 playbook 逐步執行。

---

## 7. Rollback Playbook（四階段共用）

> **適用範圍**：所有 stage 的回滾操作。  
> **目標**：安全、可觀測、可回溯。

### Step 1: Drain In-Flight Requests（不重放）

```bash
./canary-rollback.sh --drain --timeout 300
```

- 等待所有進行中的請求完成（最多 5 分鐘）。
- **不重放**任何 in-flight 請求，避免重複寫入。
- 確認 `inflight_requests_count = 0` 後進入下一步。

### Step 2: Flag Flip — Canary OFF

```bash
./canary-rollback.sh --flag-off
```

- 將 `ENABLE_M4_CANARY` 設為 `false`。
- 確認 dashboard 上 canary 流量歸零。

### Step 3: Orbit Re-Emit（Idempotency 保護）

```bash
./canary-rollback.sh --orbit-reemit --idempotent
```

- 觸發 Orbit 重新發送回滾期間可能遺漏的事件。
- Idempotency key 保證重複事件不會產生副作用。
- 確認 `orbit_reemit_success_count` 與預期一致。

### Step 4: Verify Metric 回到 Baseline

- 確認以下指標回到 canary 啟動前的 baseline：
  - 寫入成功率
  - P99 延遲
  - Error rate
  - Resource usage
- 於 Slack 發布回滾完成通知。

### Step 5: Postmortem

- 48 小時內完成 postmortem 文件。
- 記錄：觸發原因、影響範圍、timeline、root cause、action items。
- 文件存放：`docs/m4-prep/postmortems/`。

---

## 8. Hysteresis 機制

為避免 alert flapping 導致頻繁回滾，系統內建 hysteresis 保護：

- **Auto-rollback trip 後**：進入 **30 分鐘 cooldown** 期。
- Cooldown 期間內，即使指標恢復正常，**不會自動取消回滾**。
- Cooldown 結束後，需要 **oncall 工程師手動 ACK** 才能：
  - 確認回滾並結束，或
  - 取消回滾並恢復 canary（僅限指標已明確恢復且經 Chris 同意）。

```
[Alert Trigger] → [Auto-rollback initiated] → [30 min cooldown] → [Manual ACK required]
```

> ⚠️ **重要**：手動 ACK 前，oncall 必須確認 dashboard 上所有指標已穩定至少 10 分鐘。

---

## 9. 緊急 Escalation 路徑

當 rollback 失敗或出現預期外的嚴重問題時，依以下順序 escalation：

| 層級 | 聯絡對象 | 回應 SLA | 聯絡方式 |
|------|----------|----------|----------|
| L1 | **Chris** | 15 min | Slack DM + PagerDuty |
| L2 | **Kernel Team** | 30 min | PagerDuty `#kernel-oncall` |
| L3 | **Aphelion Team** | 1 hr | PagerDuty `#aphelion-escalation` |

**Escalation 條件**：
- Rollback playbook 執行失敗（Step 1-4 任一步驟 timeout）。
- Data loss > 0 且無法透過 Orbit re-emit 恢復。
- Canary 關閉後 baseline 指標未恢復（可能為 shared state corruption）。

**Escalation 訊息模板**：
```
🚨 M4 Canary ESCALATION
- Stage: @X%
- Issue: <簡述>
- Rollback status: <成功/失敗/進行中>
- Impact: <影響範圍>
- 已嘗試: <已執行的步驟>
- 需要協助: <具體需求>
```

---

*本文件為 Parallax SRE 內部操作手冊，請勿外傳。*
