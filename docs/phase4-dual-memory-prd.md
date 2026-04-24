# Phase 4 PRD — Dual-Memory Router (Aphelion × Parallax)

**狀態**: Draft
**日期**: 2026-04-18
**前置**: Phase 3 (Extract 層 + Shadow Write) 完成於 commit `82fe6b9`
**來源**: xcouncil 5-model 討論 + Opus 4.7 judge 收斂
**長期目標**: Aphelion + Parallax 永久並存為雙記憶層，Engram 為未來升級點（非本階段）

---

## 1. 為什麼要做

Phase 3 把 Parallax 以 shadow-write 並行跑起來。現在**寫入雙軌**，但**讀取只走 Aphelion** — 這不是永久架構，是過渡。

xcouncil 共識：**不做 cutover，兩邊並存**，但必須回答一個硬問題：

> 同一個 query 打過來，誰回答？衝突時聽誰的？

Phase 4 就是把這個答案實作出來。

---

## 2. 核心設計：A+ 方案（能力分流 + staged composition）

### 2.1 Query Taxonomy（硬合約，不自動分類）

呼叫方**必須**明確傳 `QueryType`，router 不做 heuristic 猜測。

| QueryType | 後端 | 範例 |
|---|---|---|
| `SEMANTIC_DISCOVERY` | Aphelion only | 「找和 X 相關的記憶」 |
| `STATE_LOOKUP` | Parallax only | 「這個 claim 現在是 confirmed 還是 rejected」 |
| `TIMELINE_AUDIT` | Parallax only | 「這個狀態是怎麼變過來的」 |
| `HYBRID_SEMANTIC_CONSTRAINED` | Aphelion 召回 → Parallax filter/hydrate | 「找和 X 相關且目前 active 的內容」 |
| `RECONCILIATION` | 雙查 diff（debug/audit only）| nightly 比對 |

### 2.2 四個能力 Port（為 Engram 預留替換點）

```
SemanticRecallPort      ← Aphelion 實作（未來 Engram 替換）
CanonicalStatePort      ← Parallax 實作
TemporalReplayPort      ← Parallax 實作
MemoryFederationPort    ← hybrid composer + reconciler（不存資料）
```

### 2.3 Hybrid 執行流程（取代 blind merge）

```
HYBRID_SEMANTIC_CONSTRAINED:
  1. Aphelion.semantic_recall(q)                   → candidates[]
  2. Parallax.hydrate_by_crosswalk(candidates) → enriched[]
  3. Parallax.filter_by_state(enriched, c)     → final[]
  4. render(final): Aphelion snippet + Parallax authority
```

### 2.4 衝突仲裁（欄位級，非全域）

| 衝突類型 | 仲裁 |
|---|---|
| state / provenance / timeline | **Parallax 贏**，無條件 |
| semantic score / snippet / markdown | **Aphelion 贏**，無條件 |
| 跨域事實不一致（同一 canonical_ref 兩邊語義衝突） | 寫 `conflict_detected` event → Parallax，回 envelope `conflict_flags`，**不靜默解決** |
| 缺 crosswalk | 分別列出，標 `ambiguous`，**禁止自動去重** |
| `zero results` | **不觸發** fallback；只有 `capability_mismatch` 或 `index_stale` 才補查 |

**關鍵洞見**：每次衝突都寫成 Parallax event — 這些 event 在 Engram 升級時直接變成 migration 的 golden dataset。

---

## 3. User Stories

### US-001: Crosswalk schema
- [ ] 新增 `crosswalk` 表（或擴充 `index_state`），欄位：
  - `canonical_ref` (PK, 格式 `memory:<id>` / `claim:<id>`)
  - `parallax_target_kind`
  - `parallax_target_id`
  - `dpkg_doc_id`（nullable）
  - `vault_path`（nullable）
  - `content_hash`
  - `source_id`
  - `last_event_id_seen`
  - `last_embedded_at`
- [ ] Migration SQL，backward compatible
- [ ] 測試：insert / query / update 都過

### US-002: QueryType enum + Router
- [ ] `parallax/router/types.py` 定義 `QueryType` enum
- [ ] `parallax/router/__init__.py` 暴露 `MemoryQueryRouter`
- [ ] Router 拒絕沒有 `query_type` 的請求（ValueError，不做自動分類）
- [ ] 單元測試：每個 QueryType 路由正確

### US-003: Capability Ports
- [ ] `parallax/router/ports.py` 定義四個 Protocol：
  - `SemanticRecallPort`
  - `CanonicalStatePort`
  - `TemporalReplayPort`
  - `MemoryFederationPort`
- [ ] Aphelion adapter 實作 `SemanticRecallPort`（包 a2a ChromaDB 呼叫）
- [ ] Parallax 內部實作 `CanonicalStatePort` + `TemporalReplayPort`
- [ ] 單元測試：每個 port 有 mock 可換

### US-004: Response Envelope
- [ ] `parallax/router/envelope.py` 定義統一回傳格式：
  ```python
  {
    "results": [...],
    "authority": "parallax" | "aphelion" | "composed",
    "provenance": {...},
    "freshness": {"index_watermark": ..., "stale": bool},
    "conflict_flags": [...],
  }
  ```
- [ ] 所有 router 出口都用這個 envelope
- [ ] 測試：envelope schema 驗證

### US-005: Hybrid executor
- [ ] 實作 `HYBRID_SEMANTIC_CONSTRAINED` 的 staged composition
- [ ] candidate → hydrate → filter → render 四階段
- [ ] 缺 crosswalk 時標 `ambiguous`，不自動去重
- [ ] 整合測試：real-ish flow with MockProvider + in-memory Aphelion stub

### US-006: Conflict detection + event log
- [ ] `parallax/router/conflict.py`：跨域事實不一致偵測
- [ ] 衝突時寫 `conflict_detected` event 到 `events` 表
- [ ] Envelope `conflict_flags` 帶 event_id
- [ ] 測試：產生衝突 → 確認 event 寫入 + envelope flag

### US-007: Reconciliation mode（離線）
- [ ] `MemoryFederationPort.reconcile(mode="nightly" | "on_demand")`
- [ ] 產出 diff report（兩邊都有 vs 只有一邊 vs 衝突）
- [ ] CLI: `parallax reconcile --since=...`
- [ ] 測試：已知 diff 能被偵測

### US-008: 觀測指標
- [ ] 每 `QueryType` 記錄：hit rate、crosswalk miss rate、stale index rate、conflict rate、hydrate latency
- [ ] 結構化 log（JSON）
- [ ] 測試：metrics emit 正確

### US-009: a2a 端整合（讀路由）
- [ ] a2a 端加 `MEMORY_ROUTER=parallax` feature flag
- [ ] flag on：讀取走 `MemoryQueryRouter`；flag off：走原 Aphelion 路徑
- [ ] 7-day canary：flag 預設 off，金絲雀 user 先開
- [ ] 寫入仍維持 `PARALLAX_DUAL_WRITE=1` 雙寫

### US-010: 文件 + 範例
- [ ] `docs/dual-memory-router.md`：架構圖 + query type 對照表 + 衝突仲裁表
- [ ] README 新增「Phase 4: Dual-Memory Router」區塊
- [ ] 範例 notebook / snippet：每個 QueryType 的典型呼叫

---

## 4. 非目標（Out of Scope）

- ❌ Engram 替換（未來升級，本階段只留介面）
- ❌ Aphelion 降為唯讀（**不做 cutover**）
- ❌ 自動 query classification（硬合約設計，禁止 heuristic）
- ❌ 預設雙查 merge（只保留給 reconcile 模式）

---

## 5. 驗收標準（Phase 4 完成條件）

1. [ ] US-001 ~ US-010 全部 `passes: true`
2. [ ] 測試覆蓋率 ≥ 80%（維持現有標準）
3. [ ] a2a 端 canary 跑滿 7 天，無 P0 regression
4. [ ] 觀測 dashboard 顯示：crosswalk miss rate < 5%，conflict rate < 1%
5. [ ] Architect/Critic 驗收通過
6. [ ] deslop pass 跑過
7. [ ] Notion 開發日誌 + 3 Lane 更新

---

## 6. 風險

| 風險 | 緩解 |
|---|---|
| Crosswalk 一開始缺資料，hybrid query 命中率低 | Phase 4 啟動前跑一次 backfill job（content_hash match） |
| Aphelion adapter 跨 repo 依賴 a2a 內部 API | 定義穩定 adapter 介面，a2a 端實作，Parallax 不 import a2a 任何東西 |
| 衝突 event 灌爆 events 表 | Rate limit + dedup by `canonical_ref + diff_type` within window |
| 讀路由 flag 切換時 cache 不一致 | 明確文件：flag 切換需重啟 a2a worker |

---

## 7. Timeline（粗估）

- Week 1: US-001 (crosswalk) + US-002 (router types) + US-003 (ports)
- Week 2: US-004 (envelope) + US-005 (hybrid executor) + US-006 (conflict)
- Week 3: US-007 (reconcile) + US-008 (metrics) + US-009 (a2a integration)
- Week 4: US-010 (docs) + canary + 驗收

---

## 8. 一句話

**Parallax 是 state/provenance/timeline 的 SoT；Aphelion 是 semantic recall 的 SoT；衝突時做欄位級仲裁並寫成 event，為 Engram 升級累積 migration 的 golden dataset。**
