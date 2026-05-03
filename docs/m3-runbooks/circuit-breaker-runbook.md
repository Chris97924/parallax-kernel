# M3 Circuit Breaker (BreakerState) 操作手冊

## 用途 / 為什麼需要

BreakerState 是 M3 架構中一個關鍵的**進程級（process-local）熔斷機制**。其主要目的是在 Aphelion 服務發生持續性不可達（unreachable）時，保護 Parallax 系統免於遭受**級聯式失敗（cascading failure）**。

當 Aphelion 出現問題時，Parallax 的 dual-read 流程（同時讀取 Aphelion 與 Parallax 本地數據）會因等待逾時而大量堆積，最終耗盡資源，導致整個服務不可用。BreakerState 透過監控 Aphelion 的不可達率，在問題惡化前主動「熔斷」，將流量**降級（fallback）為僅讀取 Parallax 本地數據**，從而保障核心讀路徑的可用性與穩定性。

## 觸發條件

BreakerState 採用**5 分鐘滾動視窗**進行計算，並同時滿足以下兩個條件時才會觸發（Trip）：

1.  **不可達率（Unreachable Rate）**：在滾動視窗內，對 Aphelion 的請求失敗率 **> 1%**。
2.  **最低觀察數（Minimum Observations）**：在滾動視窗內，已累積至少 **50 次**觀察記錄。

此設計（特別是最低觀察數）是為了防止在服務啟動初期（Cold-start）因少量請求失敗而導致**不必要的抖動（flap）**。

## Trip 後的 Dual-Read 行為

一旦 BreakerState Trip，所有進程內的 dual-read 操作將立即改變行為：
- **停止**向 Aphelion 發送任何讀取請求。
- **完全降級**為僅從 Parallax 本地數據源讀取。
- 此狀態將持續，直到管理員**手動執行重置（Reset）**。系統**不會自動恢復**。

## Oncall 偵測

值班工程師應透過以下方式偵測熔斷事件：

1.  **關鍵指標上升**：監控 `parallax_circuit_breaker_tripped_total` 計數器。這是一個**單調遞增（monotonic）** 的計數器，其數值上升即代表發生了一次新的 Trip 事件。
2.  **Grafana 面板**：在對應的 Grafana Dashboard 中，應有專門面板顯示此計數器的變化趨勢以及 BreakerState 的當前狀態（例如：`CLOSED` / `OPEN`）。當面板顯示狀態變為 `OPEN` 且計數器上升時，即確認熔斷已觸發。

## 為什麼是手動 Reset

根據 **Q10 ralplan** 的設計決策，BreakerState **不提供自動恢復（auto-recovery）** 功能。主要原因如下：

在 Aphelion 出現間歇性抖動（thrashing）或區域性故障的場景下，自動恢復機制可能導致系統在「熔斷」與「嘗試連接」之間快速反覆切換。這種反覆切換本身會放大事件影響，造成流量波動、資源浪費，並使根本原因更難被定位。手動重置強制要求運維人員**確認根本原因已解決後**，才恢復服務，這是一種更為審慎和可控的故障處理方式。

## Reset 步驟

執行重置前，請務必遵循以下步驟，切勿盲目重置：

1.  **確認 Aphelion 服務健康**：
    ```bash
    curl -I http://<aphelion-endpoint>/health
    ```
    確認返回 HTTP 200 OK，且服務端點回應正常。

2.  **確認根本原因已解決**：
    檢查 Aphelion 服務的監控、日誌及相關告警，確認導致不可達的問題（如網路中斷、服務崩潰、資源耗盡等）**已明確瞭解並修復**。

3.  **透過管理端點觸發重置**：
    向 Parallax 的管理端點發送 POST 請求以重置熔斷器。
    ```bash
    curl -X POST http://<parallax-admin-endpoint>/admin/circuit-breaker/reset
    ```
    預期收到成功回應（如 HTTP 200）。

4.  **觀察 Inflight Gauge 復原**：
    重置後，密切監控 Grafana 中與 dual-read 相關的 **inflight 請求數量（gauge）**。應觀察到該指標從接近零的狀態開始緩慢上升，這表明系統正在重新嘗試向 Aphelion 發送請求。

## Reset 後驗證

重置成功後，需進行以下驗證：

1.  **連接成功率**：檢查 Aphelion 的連接成功率指標，應迅速回升至接近 100%。
2.  **首批 Dual-Read 成功率**：觀察重置後**前 100 次** dual-read 操作的成功率。目標是成功率應非常高（例如 >99%），這能確認 Aphelion 連接已穩定恢復。

## 異常情境：Reset 後立即又 Trip

如果在執行重置後，`parallax_circuit_breaker_tripped_total` 計數器**在短時間內再次上升**，表明熔斷器被再次觸發。

**這強烈意味著根本原因並未真正解決**。此時：
- **禁止**再次執行重置操作。
- **立即將事件升級（Escalate）**，聯合 Aphelion 團隊進行深入調查。
- 持續監控，讓 BreakerState 保持在熔斷狀態，以保護 Parallax 核心服務。
