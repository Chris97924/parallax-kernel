```markdown
# M3 Lane C v0.3 Dual-read — Graceful Drain Handler Runbook

> **文件編號**：`docs/m3-runbooks/q8-drain-runbook.md`
> **適用版本**：M3 dual-read（Lane C v0.3）
> **最後更新**：2026-04-27
> **關聯 Ralplan**：2026-04-27 §10 Q8
> **Owner**：Parallax SRE

---

## 用途 / 觸發情境

本 runbook 覆蓋 M3 dual-read 模式下，**deploy 或 restart** 時的 graceful drain 流程。

觸發情境：

| 情境 | 說明 |
|------|------|
| **Rolling deploy** | 新版本 Pod 就緒後，舊版本進入 drain |
| **手動 restart** | `systemctl restart parallax-dual-read` 或 pm2 restart |
| **OOM / CrashLoop** | 進程異常退出，需確認 inflight 是否已歸零 |
| **Scale-in** | HPA 或手動縮容，淘汰舊實例 |

核心原則：**新請求路由到新版本，舊版本只完成 in-flight 請求後收尾**。不使用 250 LOC 的完整 drain module，改以 30 LOC inflight gauge + 本 runbook 取代（節省 220 LOC + 2 天工時）。

---

## Drain 行為定義（30 LOC Inflight Gauge 取代方案）

舊方案（已棄用）：250 LOC drain module，內建 request interceptor、shutdown hook、graceful drain loop。

**新方案**：僅依賴一個 Prometheus gauge：

```
parallax_dual_read_inflight{instance="<pod>"}
```

行為邏輯：

1. **進程啟動**：gauge 初始化為 0。
2. **請求進入**：handler entry point 執行 `inflight.inc()`。
3. **請求完成**（成功或失敗）：handler exit 執行 `inflight.dec()`。
4. **收到 SIGTERM**：進程停止接受新請求（由 load balancer / reverse proxy 層處理），但**不主動 kill in-flight**。
5. **inflight 降為 0**：進程自行退出，或由 runbook 中的 oncall 動作介入。

> **設計取捨**：不實作 drain loop，改以可觀測性驅動。oncall 透過 gauge 判斷 drain 狀態，必要時強制介入。這在 M3 dual-read 的低並發場景下足夠。

---

## 觀察指標：parallax_dual_read_inflight 何時降到 0

| 條件 | 預期 drain 時間 |
|------|----------------|
| 正常流量（p99 latency < 500ms） | **< 10 秒** |
| 高延遲查詢（p99 ~ 2s） | **< 15 秒** |
| 含外部 IO 回調（DB / RPC） | **< 30 秒** |
| 異常：inflight 卡住 | **> 60 秒 → 進入異常處理** |

**觀察方式**：

```bash
# 即時觀察
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=parallax_dual_read_inflight{job="m3-dual-read"}' | jq

# Grafana 面板
# Dashboard: M3 Dual-Read → Panel: Inflight Requests
```

---

## Oncall 動作

### 步驟 1：觀察 Inflight Gauge

```bash
# 確認目標實例
INSTANCES=$(curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=parallax_dual_read_inflight{job="m3-dual-read"} > 0' | jq -r '.data.result[].metric.instance')

echo "尚有 inflight 的實例：$INSTANCES"
```

若所有實例 inflight = 0，drain 已完成，無需後續動作。

### 步驟 2：等待降至 0（Hard Timeout 90s）

```bash
TIMEOUT=90
ELAPSED=0

while [ $ELAPSED -lt $TIMEOUT ]; do
  COUNT=$(curl -s http://localhost:9090/api/v1/query \
    --data-urlencode 'query=sum(parallax_dual_read_inflight{job="m3-dual-read"})' \
    | jq '.data.result[0].value[1]' | tr -d '"')

  if [ "$COUNT" = "0" ] || [ "$COUNT" = "NaN" ]; then
    echo "✅ Drain 完成，耗時 ${ELAPSED}s"
    exit 0
  fi

  echo "⏳ Inflight: $COUNT，已等待 ${ELAPSED}s..."
  sleep 5
  ELAPSED=$((ELAPSED + 5))
done

echo "❌ Drain timeout！進入步驟 3"
```

### 步驟 3：超時 → 強制終止 + 事件記錄

```bash
# 強制終止
kill -9 <pid>
# 或
systemctl kill -s KILL parallax-dual-read

# 寫入事件記錄（供事後 postmortem）
cat >> /var/log/parallax/drain-events.jsonl <<EOF
{"ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","event":"drain_timeout","instance":"$INSTANCE","inflight_at_timeout":$COUNT,"action":"SIGKILL","operator":"$USER"}
EOF
```

---

## 異常處理：Drain Timeout / Inflight 卡住不降

### 狀態：inflight 長時間不降（> 60s）

**可能原因與處置**：

| 原因 | 診斷 | 處置 |
|------|------|------|
| 請求 hang（外部服務無回應） | 查看 `parallax_dual_read_request_duration_seconds_count` 是否停止遞增 | 設定上游 timeout（建議 30s），或強制 kill |
| Gauge 計數 bug（inc/dec 不配對） | 查 code review，grep `inflight.inc` vs `inflight.dec` | Hotfix：確保所有 exit path 都有 dec |
| 連線池洩漏 | `ss -tnp` 檢查 ESTABLISHED 連線數 | 重啟進程 + 排查 connection leak |
| 死鎖 | `kill -SIGQUIT <pid>` 取 thread dump | 分析 thread dump，修復後 redeploy |

### 緊急繞過

若 drain 持續卡住且影響 deploy pipeline：

```bash
# 設定環境變數跳過 drain 等待（僅限緊急）
DRAIN_HARD_TIMEOUT=10 systemctl restart parallax-dual-read
```

---

## 驗證 Drain 成功：Post-Deploy Probes

Drain 完成後，執行以下驗證：

```bash
# 1. Inflight 歸零確認
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=parallax_dual_read_inflight{job="m3-dual-read"}' \
  | jq '.data.result[].value[1]' | grep -q '"0"' && echo "✅ Inflight = 0"

# 2. 新版本 readiness
curl -sf http://localhost:8080/healthz && echo "✅ Healthz OK"

# 3. Dual-read 一致性 smoke test
curl -s http://localhost:8080/api/v1/dual-read/probe \
  | jq '.lane_c_consistent' | grep -q true && echo "✅ Dual-read 一致"

# 4. 錯誤率無異常
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=rate(parallax_dual_read_errors_total[1m])' \
  | jq '.data.result[0].value[1]' | tr -d '"'
# 預期：接近 0
```

---

## 回滾：Drain 未完成即部署的應變

若新版本在舊版本 drain 完成前就已部署（pipeline 誤判或手動操作失誤）：

1. **立即暫停 pipeline**：
   ```bash
   kubectl rollout pause deployment/m3-dual-read -n parallax
   ```

2. **回退到舊版本**：
   ```bash
   kubectl rollout undo deployment/m3-dual-read -n parallax
   # 或指定 revision
   kubectl rollout undo deployment/m3-dual-read -n parallax --to-revision=<N>
   ```

3. **確認回退後 inflight 正常**：
   ```bash
   watch -n 2 'curl -s http://localhost:9090/api/v1/query \
     --data-urlencode "query=parallax_dual_read_inflight" | jq ".data.result"'
   ```

4. **事件記錄**：
   ```json
   {"ts":"...","event":"rollback_drain_incomplete","reason":"drain_not_finished_before_deploy","action":"rollout_undo"}
   ```

> **預防措施**：在 CI/CD pipeline 中加入 drain gate——deploy job 等待 `parallax_dual_read_inflight == 0` 或 hard timeout 90s 後才 proceed。

---

## 與 systemd / pm2 整合

### systemd 建議

```ini
# /etc/systemd/system/parallax-dual-read.service
[Service]
Type=notify
ExecStart=/usr/local/bin/parallax-dual-read
KillSignal=SIGTERM

# 給 drain 的寬限時間（90s hard timeout + 10s buffer）
TimeoutStopSec=100
# Reload 時也給足夠時間
TimeoutStartSec=30

# 確保 SIGTERM 後有足夠時間 drain
SendSIGKILL=yes
KillMode=mixed
```

| 參數 | 建議值 | 說明 |
|------|--------|------|
| `TimeoutStopSec` | **100s** | 90s drain timeout + 10s buffer |
| `TimeoutStartSec` | **30s** | 啟動超時 |
| `KillSignal` | **SIGTERM** | 先 graceful，再 SIGKILL |
| `KillMode` | **mixed** | 先 SIGTERM 主進程，超時後 SIGKILL 全部 |

### pm2 建議

```javascript
// ecosystem.config.js
module.exports = {
  apps: [{
    name: 'parallax-dual-read',
    script: './dist/index.js',
    kill_timeout: 10000,   // 10s（pm2 預設 SIGKILL 前等待）
    listen_timeout: 10000,
    shutdown_with_message: true,  // 支持 graceful shutdown via IPC
    // 注意：pm2 的 kill_timeout 上限較低，
    // 若需 90s drain 建議改用 systemd
  }]
};
```

> **⚠️ 注意**：pm2 的 `kill_timeout` 最大實務值約 15-30s。若 drain 需要更長時間，**強烈建議使用 systemd**，其 `TimeoutStopSec` 可設至 100s 以上。

---

## 附錄：快速決策樹

```
Deploy / Restart 觸發
  │
  ├─ SIGTERM 發送 → 舊版本停止接受新請求
  │
  ├─ 觀察 parallax_dual_read_inflight
  │     │
  │     ├─ < 90s 降到 0 → ✅ Drain 成功
  │     │
  │     └─ ≥ 90s 仍 > 0 → ❌ Drain timeout
  │           │
  │           ├─ 記錄事件
  │           ├─ SIGKILL 強制終止
  │           └─ 觸發 postmortem（若為首次）
  │
  └─ Post-deploy probes → 確認新版本健康
```
```

---

*本文件由 Parallax SRE 維護。如有疑問，於 #parallax-sre 頻道聯繫 oncall。*
