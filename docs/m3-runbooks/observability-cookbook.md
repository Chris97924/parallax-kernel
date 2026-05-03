```markdown
# M3 Observability Cookbook

> **文件版本**：v0.3.1　｜　**最後更新**：2026-04-30  
> **適用範圍**：Lane C v0.3 Dual-read（M3）  
> **維護者**：Parallax SRE — M3 oncall rotation

---

## 1　用途與受眾

本文件為 **M3 oncall 工程師** 的日常操作手冊，目標是：

1. 將 6 個 DoD gauges、15 個 Grafana panels、5 條 Prometheus alert rules 串成一條可執行的觀測鏈。
2. 提供三大常見異常場景的標準排查 SOP，降低 MTTR。
3. 在 14-day corpus 觀察期（day 0 = 2026-04-30）內，定義每日 health check 動作。

**閱讀前提**：你應已讀過 `q8-drain-runbook.md` 與 `circuit-breaker-runbook.md`，並擁有 Grafana M3 dashboard 與 Prometheus alertmanager 的唯讀權限。

---

## 2　DoD Gauges 速查表

| # | Gauge 名稱 | 狀態 | 類型 | Threshold（alert 觸發） | 說明 |
|---|-----------|------|------|------------------------|------|
| 1 | `dual_read_discrepancy_rate` | ✅ 已部署 | Gauge | P0 > 0.3 % | 雙讀結果不一致比率 |
| 2 | `arbitration_conflict_rate` | ✅ 已部署 | Gauge | P0 > 1.5 % | 仲裁引擎無法自動裁決的衝突比率 |
| 3 | `dual_read_write_error_rate` | ✅ 已部署 | Gauge | P1 > 0.05 % | 雙寫路徑寫入失敗比率 |
| 4 | `aphelion_unreachable_rate` | ⏳ 未部署 | Gauge | P1 > 1 % sustained 5 min | Aphelion 節點不可達比率 |
| 5 | `crosswalk_miss_rate` | ⏳ 未部署 | Gauge | —（觀察用） | Crosswalk lookup miss 比率 |
| 6 | `circuit_open_count_72h` | ⏳ 未部署 | Gauge | P2 > 3 | 過去 72 小時斷路器跳脫次數 |

> **備註**：未部署 gauges 在 Grafana 中以 `N/A` placeholder panel 呈現，待指標上線後自動啟用。`crosswalk_miss_rate` 目前僅供趨勢觀察，尚無 alert rule。

---

## 3　Grafana 15 Panels 分組

Dashboard URL：`https://grafana.internal/d/m3-lane-c-v03/`

### Block A — 即時指標（Row 1，3 panels）

| Panel | 指標來源 | 顯示方式 | 說明 |
|-------|---------|---------|------|
| A-1 | `dual_read_discrepancy_rate` | Stat + sparkline | 即時雙讀不一致率，紅色閾值線 0.3 % |
| A-2 | `arbitration_conflict_rate` | Stat + sparkline | 即時仲裁衝突率，紅色閾值線 1.5 % |
| A-3 | `dual_read_write_error_rate` | Stat + sparkline | 即時雙寫錯誤率，黃色閾值線 0.05 % |

> **操作提示**：Block A 是 oncall 每次 alert 觸發時第一眼看到的區域。若三個 panel 均為綠色，可快速排除 M3 本身問題。

### Block B — 趨勢與比較（Row 2–3，6 panels）

| Panel | 指標來源 | 顯示方式 | 說明 |
|-------|---------|---------|------|
| B-1 | `dual_read_discrepancy_rate` | Time series（72h rolling） | 72 小時滾動趨勢，疊加 deploy marker |
| B-2 | `arbitration_conflict_rate` | Time series（72h rolling） | 同上，仲裁衝突趨勢 |
| B-3 | `dual_read_write_error_rate` | Time series（72h rolling） | 同上，寫入錯誤趨勢 |
| B-4 | `aphelion_unreachable_rate` | Time series（72h rolling） | ⏳ placeholder，待部署 |
| B-5 | `crosswalk_miss_rate` | Time series（72h rolling） | ⏳ placeholder，待部署 |
| B-6 | `circuit_open_count_72h` | Bar chart（14d corpus 進度） | 斷路器跳脫次數 vs. corpus 觀察期日曆 |

> **操作提示**：B-6 panel 同時顯示 14-day corpus 觀察期的 day counter，方便快速定位當前處於觀察期第幾天。

### Block C — 衍生分析（Row 4–6，6 panels）

| Panel | 指標來源 | 顯示方式 | 說明 |
|-------|---------|---------|------|
| C-1 | `dual_read_discrepancy_rate` by query_type | Heatmap | 依 query type 拆分的不一致率熱力圖 |
| C-2 | `arbitration_conflict_rate` by result | Pie chart | 仲裁結果分佈（auto-resolved / manual / timeout） |
| C-3 | `dual_read_write_error_rate` by endpoint | Table | 依寫入端點拆分的錯誤率明細 |
| C-4 | `dual_read_discrepancy_rate` vs `arbitration_conflict_rate` | Correlation scatter | 雙讀不一致 vs 仲裁衝突相關性 |
| C-5 | `aphelion_unreachable_rate` by node | Table | ⏳ placeholder，待部署 |
| C-6 | `circuit_open_count_72h` timeline | Event timeline | 斷路器跳脫事件時間線，疊加 deploy / drain 事件 |

> **操作提示**：排查場景 A/B 時，先看 C-1/C-2 定位問題 query type 或仲裁規則，再回溯 Block B 趨勢確認是否為突發或漸進。

---

## 4　Prometheus Alert Rules 與 Severity 分級

```yaml
# prometheus/rules/m3_alerts.yml

groups:
  - name: m3_lane_c_alerts
    rules:
      # ── P0 Hot ──────────────────────────────────────
      - alert: M3DiscrepancyRateHigh
        expr: dual_read_discrepancy_rate > 0.003
        for: 1m
        labels:
          severity: p0-hot
          team: m3-oncall
        annotations:
          summary: "M3 雙讀不一致率 > 0.3%"
          runbook: "docs/m3-runbooks/observability-cookbook.md#場景-a-discrepancy-spike"

      - alert: M3ArbitrationConflictHigh
        expr: arbitration_conflict_rate > 0.015
        for: 1m
        labels:
          severity: p0-hot
          team: m3-oncall
        annotations:
          summary: "M3 仲裁衝突率 > 1.5%"
          runbook: "docs/m3-runbooks/observability-cookbook.md#場景-b-arbitration-conflict"

      # ── P1 Warm ─────────────────────────────────────
      - alert: M3WriteErrorRateHigh
        expr: dual_read_write_error_rate > 0.0005
        for: 2m
        labels:
          severity: p1-warm
          team: m3-oncall
        annotations:
          summary: "M3 雙寫錯誤率 > 0.05%"
          runbook: "docs/m3-runbooks/observability-cookbook.md#場景-a-discrepancy-spike"

      - alert: M3AphelionUnreachableSustained
        expr: aphelion_unreachable_rate > 0.01
        for: 5m
        labels:
          severity: p1-warm
          team: m3-oncall
        annotations:
          summary: "M3 Aphelion 不可達率 > 1% 持續 5 分鐘"
          runbook: "docs/m3-runbooks/circuit-breaker-runbook.md"

      # ── P2 Page Review ──────────────────────────────
      - alert: M3CircuitBreakerTripsHigh
        expr: circuit_open_count_72h > 3
        for: 0m
        labels:
          severity: p2-page-review
          team: m3-oncall
        annotations:
          summary: "M3 斷路器 72h 內跳脫 > 3 次"
          runbook: "docs/m3-runbooks/circuit-breaker-runbook.md"
```

### Severity 定義

| 級別 | 通知管道 | 回應 SLA | 說明 |
|------|---------|---------|------|
| **P0 hot** | PagerDuty + Slack #m3-incidents | 5 分鐘內 ack | 直接影響線上雙讀正確性，需立即介入 |
| **P1 warm** | Slack #m3-alerts + email | 15 分鐘內 ack | 錯誤率偏高但尚未觸發斷路器，需儘速排查 |
| **P2 page review** | Slack #m3-alerts（no page） | 下一工作日 review | 斷路器頻繁跳脫，需安排根因分析 |

---

## 5　三大 Oncall 場景應對

### 場景 A：Discrepancy Spike

**觸發條件**：`M3DiscrepancyRateHigh`（P0）

**排查步驟**：

1. **確認範圍** → 打開 Block A-1，確認 spike 時間點與持續時間。
2. **定位 query type** → 切到 Block C-1 熱力圖，找出 discrepancy 集中的 query type。
3. **檢查路由** → 查詢 `route_decision_log`，確認是否有非預期的路由切換（例如 Aphelion fallback 觸發）。
4. **比對雙寫一致性** → 檢查 Block A-3 write error rate，若同步升高，優先排查雙寫路徑。
5. **查看 deploy marker** → Block B-1 是否有近期 deploy 對應 spike。
6. **決策**：
   - 若為單一 query type 且 discrepancy < 0.5 % → 持續觀察 15 分鐘。
   - 若 discrepancy > 0.5 % 或擴散至多 query type → 啟動 `q8-drain-runbook.md` drain 流程。

### 場景 B：Arbitration Conflict

**觸發條件**：`M3ArbitrationConflictHigh`（P0）

**排查步驟**：

1. **確認衝突分佈** → Block C-2 pie chart，看 auto-resolved vs manual vs timeout 比例。
2. **定位欄位** → 查詢 `arbitration_detail_log`，找出衝突集中的欄位名稱。
3. **比對 source-level mismatch** → 檢查上游資料源是否有 schema 變更或資料延遲。
4. **檢查仲裁規則** → 確認 `arbitration_rules.yaml` 中對應欄位的優先序是否仍合理。
5. **決策**：
   - 若 auto-resolved > 90 % → 記錄 event，觀察。
   - 若 manual/timeout > 10 % → 升級至 M3 platform team，必要時暫停該欄位的雙讀。

### 場景 C：Circuit Breaker Trip

**觸發條件**：`M3CircuitBreakerTripsHigh`（P2）或 `M3AphelionUnreachableSustained`（P1）

**排查步驟**：

1. **確認跳脫時間線** → Block C-6 event timeline，定位每次跳脫的精確時間。
2. **交叉比對** → Block B-4 aphelion unreachable rate（若已部署），確認是否為 Aphelion 節點問題。
3. **執行 circuit-breaker-runbook** → 依照 `circuit-breaker-runbook.md` 的標準流程：
   - 確認 circuit state（open / half-open / closed）。
   - 若為 open → 等待 cooldown 或手動 reset。
   - 若為 half-open → 監控 probe 成功率。
4. **記錄根因** → 填寫 incident log，標記是否需要調整 circuit breaker threshold。

---

## 6　14-Day Corpus 觀察期（Day 0 = 2026-04-30）

在觀察期內，oncall 工程師需於 **每日 UTC 09:00** 執行以下 health check：

| 步驟 | 動作 | 預期結果 | 異常處理 |
|------|------|---------|---------|
| 1 | 檢查 Block A 三個即時指標 | 全部綠色 | 若任一紅色 → 依場景 A/B/C 處理 |
| 2 | 檢查 Block B-6 day counter | Day N 與日曆吻合 | 若不符 → 確認 deploy pipeline |
| 3 | 檢查 Block B-1~B-3 趨勢 | 無明顯上升趨勢 | 若有上升 → 記錄並標記為 watch item |
| 4 | 檢查 Block C-1 熱力圖 | 無新 query type 出現異常 | 若有 → 新增至 watch list |
| 5 | 檢查 `circuit_open_count_72h` | ≤ 3 | 若 > 3 → P2 alert 已觸發，安排 review |
| 6 | 更新觀察期 tracker spreadsheet | 記錄當日 status | — |

> **觀察期結束條件**（Day 14 = 2026-05-13）：
> - 所有已部署 gauges 連續 14 天無 P0 alert。
> - `dual_read_discrepancy_rate` 72h rolling 均值 < 0.1 %。
> - `circuit_open_count_72h` 累計 ≤ 5。
>
> 若未達標，觀察期自動延長 7 天，並通知 M3 platform team。

---

## 7　與其他 Runbook 連動

| Runbook | 觸發時機 | 連動方式 |
|---------|---------|---------|
| **`q8-drain-runbook.md`** | 場景 A 中 discrepancy > 0.5 % 且持續擴散 | 啟動 Q8 drain 流程，將流量從 Lane C 切回 Lane A/B；drain 期間 Block A 指標應逐步回落 |
| **`circuit-breaker-runbook.md`** | 場景 C 斷路器跳脫 | 依照 circuit breaker state machine 操作；reset 後需觀察 Block B-4/B-6 至少 30 分鐘確認穩定 |

> **連動原則**：執行 drain 或 circuit breaker reset 後，oncall 工程師需在 Slack #m3-incidents 發布狀態更新，並在 incident log 中記錄操作時間與觀察結果。

---

## 附錄：快速連結

- Grafana Dashboard：`https://grafana.internal/d/m3-lane-c-v03/`
- Prometheus Alerts：`https://prometheus.internal/alerts?group=m3_lane_c_alerts`
- Incident Log Template：`https://wiki.internal/m3/incident-log-template`
- Observability Cookbook（本文件）：`docs/m3-runbooks/observability-cookbook.md`

---

*本文件由 Parallax SRE 維護。如有問題，請於 Slack #m3-oncall 提出。*
```
