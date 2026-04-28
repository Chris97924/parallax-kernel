# Phase 4 PRD — Dual-Memory Router (Aphelion × Parallax)

**狀態**: Draft（2026-04-18 原稿）
**日期**: 2026-04-18
**前置**: Phase 3 (Extract 層 + Shadow Write) 完成於 commit `82fe6b9`
**來源**: xcouncil 5-model 討論 + Opus 4.7 judge 收斂
**長期目標**: Aphelion + Parallax 永久並存為雙記憶層，Engram 為未來升級點（非本階段）

> **更新 (2026-04-24)**：Lane D-1（MEMORY_ROUTER contract freeze, PR #2）+ Lane D-2（Real adapter interface, PR #3）已 ship。本 PRD 的 `QueryType` enum 與 Port 命名已對齊 code：
> - `QueryType` 5-value closed set — `parallax/router/types.py`
> - 四個能力 Port — `parallax/router/ports.py`
> - MockMemoryRouter / RealMemoryRouter — `parallax/router/{mock,real}_adapter.py`
>
> Routing dispatch（哪個 QueryType 走哪個 backend）與 hybrid composer 實作留在 Lane D-3。

---

## 1. 為什麼要做

Phase 3 把 Parallax 以 shadow-write 並行跑起來。現在**寫入雙軌**，但**讀取只走 Aphelion** — 這不是永久架構，是過渡。

xcouncil 共識：**不做 cutover，兩邊並存**，但必須回答一個硬問題：

> 同一個 query 打過來，誰回答？衝突時聽誰的？

Phase 4 就是把這個答案實作出來。

---

## 2. 核心設計：A+ 方案（能力分流 + staged composition）

### 2.1 Query Taxonomy（硬合約，不自動分類）

呼叫方**必須**明確傳 `QueryType`，router 不做 heuristic 猜測。Lane D-1 contract freeze 的 5-value closed set（見 `parallax/router/types.py`）：

| QueryType | 用途 | 範例 |
|---|---|---|
| `RECENT_CONTEXT` | 近期對話 + 多 session 連續性 | 「剛剛討論的那個 bug」 |
| `ARTIFACT_CONTEXT` | 檔案 / 路徑 / artifact 記憶 | 「我在 parallax/router/*.py 寫了什麼」 |
| `ENTITY_PROFILE` | Entity profile（user_fact / preference / named entity） | 「Chris 偏好什麼 stack」 |
| `CHANGE_TRACE` | 決策 + bug fix（變更歷史） | 「這個欄位是怎麼演進過來的」 |
| `TEMPORAL_CONTEXT` | when / before / after 時間窗查詢 | 「2026-04 之前發生過什麼」 |

Backend 分派（哪個 QueryType 走 Aphelion / Parallax / hybrid）由 Lane D-3 routing policy 定義；interface-freeze dispatch table 占位在 `parallax/router/real_adapter.py::QUERY_DISPATCH`。Reconciliation（跨源 diff 稽核）為獨立 port，不是 `QueryType` enum 值，見 §US-007。

### 2.2 四個能力 Port（Lane D-1 contract freeze）

見 `parallax/router/ports.py`，皆為 `@runtime_checkable` `Protocol`：

```
QueryPort      ← query(QueryRequest)     -> RetrievalEvidence
IngestPort     ← ingest(IngestRequest)   -> IngestResult
InspectPort    ← health()                -> HealthReport
BackfillPort   ← backfill(BackfillRequest) -> BackfillReport
```

兩個 adapter 都實作四個 port：`MockMemoryRouter`（Lane D-1 freeze，全部 `NotImplementedError`）、`RealMemoryRouter`（Lane D-2 freeze，dispatch 占位）。Engram 升級時替換 adapter，port 介面不動。

### 2.3 Hybrid 執行流程（Lane D-3 待實作）

Lane D-1 contract freeze 的 `QueryType` 是 5-value closed set，不含獨立的 hybrid enum 值。Hybrid composer（跨源 recall → hydrate → filter → render）由 Lane D-3 routing layer 實作，作為特定 `QueryType` 的 dispatch strategy，而非新的 enum 值。

原始設計 sketch（保留為 Lane D-3 實作參考）：

```
semantic-constrained hybrid composer:
  1. semantic_recall(q)                        → candidates[]
  2. hydrate_by_crosswalk(candidates)          → enriched[]
  3. filter_by_state(enriched, constraints)    → final[]
  4. render(final): semantic snippet + state authority
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
  - `aphelion_doc_id`（nullable）
  - `vault_path`（nullable）
  - `content_hash`
  - `source_id`
  - `last_event_id_seen`
  - `last_embedded_at`
- [ ] Migration SQL，backward compatible
- [ ] 測試：insert / query / update 都過

### US-002: QueryType enum + Router
- [ ] `parallax/router/types.py` 定義 `QueryType` enum（5-value closed set）
- [ ] `parallax/router/__init__.py` 暴露 `MockMemoryRouter`（Lane D-1）+ `RealMemoryRouter`（Lane D-2）
- [ ] `QueryRequest` 以 frozen dataclass 保證 `query_type` 必填（漏傳則 construction 階段 `TypeError`）；router layer 不做自動分類 heuristic
- [ ] 單元測試：每個 QueryType 路由正確（實作在 Lane D-3）

### US-003: Capability Ports
- [ ] `parallax/router/ports.py` 定義四個 `@runtime_checkable` Protocol：
  - `QueryPort`    — `query(QueryRequest) -> RetrievalEvidence`
  - `IngestPort`   — `ingest(IngestRequest) -> IngestResult`
  - `InspectPort`  — `health() -> HealthReport`
  - `BackfillPort` — `backfill(BackfillRequest) -> BackfillReport`
- [ ] `MockMemoryRouter`（Lane D-1）+ `RealMemoryRouter`（Lane D-2）各自實作四個 port
- [ ] Aphelion / Parallax backend-specific dispatch 在 Lane D-3 routing policy 決定
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

### US-005: Hybrid executor（Lane D-3 scope）
- [ ] 實作 semantic-constrained hybrid composer（對 applicable `QueryType` 觸發，見 §2.3）— 非新 enum 值
- [ ] candidate → hydrate → filter → render 四階段
- [ ] 缺 crosswalk 時標 `ambiguous`，不自動去重
- [ ] 整合測試：real-ish flow with MockProvider + in-memory semantic recall stub

### US-006: Conflict detection + event log
- [ ] `parallax/router/conflict.py`：跨域事實不一致偵測
- [ ] 衝突時寫 `conflict_detected` event 到 `events` 表
- [ ] Envelope `conflict_flags` 帶 event_id
- [ ] 測試：產生衝突 → 確認 event 寫入 + envelope flag

### US-007: Reconciliation mode（離線）— **DEFERRED to v0.4+**

> **2026-04-27 update:** This US-007 (Reconciliation) is **superseded** in numbering by M3's renumbered work (see `.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md`). The reconciliation capability remains a roadmap item but moves to v0.4+ scope. M3 Lane C uses **US-011 = Dual-read router** and **US-012 = Field arbitration engine** (the 2026-04-27 milestone spec; see plan §2 Q6 for context).

- [ ] [DEFERRED] Reconciliation 能力（Lane D-3 scope）— Lane D-1 contract freeze 未納入，實作入口待 routing layer 定義（候選：獨立 port 或 `BackfillPort` 的 mode）
- [ ] [DEFERRED] 產出 diff report（兩邊都有 vs 只有一邊 vs 衝突）
- [ ] [DEFERRED] CLI: `parallax reconcile --since=...`
- [ ] [DEFERRED] 測試：已知 diff 能被偵測

### US-008: 觀測指標 — **PARTIALLY ABSORBED into M2 WS-3 + M3 DoD gauges**

> **2026-04-27 update:** This US-008 (per-QueryType metrics) is **superseded** in numbering. M2 WS-3 (PR #21) shipped `parallax_shadow_*` gauges; M3 ships `parallax_dual_read_*` + `parallax_arbitration_*` + `parallax_aphelion_unreachable_rate` + `parallax_crosswalk_miss_rate` (see ralplan §6). The "per-QueryType" cut of these metrics remains a v0.4 follow-up. M3 Lane C arbitration engine is at **US-012** (renumbered).

- [ ] [PARTIAL → M2 WS-3 + M3 §6] 每 `QueryType` 記錄：hit rate、crosswalk miss rate、stale index rate、conflict rate、hydrate latency
- [ ] [DEFERRED to v0.4] 結構化 log（JSON）per QueryType cut
- [ ] [DEFERRED to v0.4] 測試：metrics emit 正確（per-QueryType cut）

### US-009: a2a 端整合（讀路由）
- [ ] a2a 端加 `MEMORY_ROUTER=parallax` feature flag
- [ ] flag on：讀取走 `RealMemoryRouter`；flag off：走原 Aphelion 路徑
- [ ] 7-day canary：flag 預設 off，金絲雀 user 先開
- [ ] 寫入仍維持 `PARALLAX_DUAL_WRITE=1` 雙寫

### US-010: 文件 + 範例
- [ ] `docs/dual-memory-router.md`：架構圖 + query type 對照表 + 衝突仲裁表
- [ ] README 新增「Phase 4: Dual-Memory Router」區塊
- [ ] 範例 notebook / snippet：每個 QueryType 的典型呼叫

### US-011: Dual-read 路由器（M3, 2026-04-27 milestone）
- 完整 spec 與 task breakdown 在 [`.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md`](../.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md) §3
- DoD: 72h `discrepancy_rate < 0.1%` + `aphelion_unreachable_rate < 0.5%` + `write_error_rate < 0.02%` + `crosswalk_miss_rate < 5%`
- Status: BLOCKED on M2 squash + Q1-Q16 answers (see ralplan §10)

### US-012: 欄位仲裁 engine（M3, 2026-04-27 milestone）
- 完整 spec 與 task breakdown 在 [`.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md`](../.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md) §3
- DoD: 72h `arbitration_conflict_rate < 1%`
- Status: BLOCKED on US-011 ship + 24h canary clean + Q1 (granularity) answer
- Antithesis open (Architect): consider ship US-011-only first, harvest 2 weeks divergence corpus, redesign US-012 from data

---

## 4. 非目標（Out of Scope）

- ❌ Engram 替換（未來升級，本階段只留介面）
- ❌ Aphelion 降為唯讀（**不做 cutover**）
- ❌ 自動 query classification（硬合約設計，禁止 heuristic）
- ❌ 預設雙查 merge（只保留給 reconcile 模式）

---

## 5. 驗收標準（Phase 4 完成條件）

1. [ ] **Phase 4 shipped 範圍** 全部 `passes: true`：US-001、US-002、US-003、US-004、US-005、US-006、US-009、US-010
   - US-007 (Reconciliation) **DEFERRED to v0.4+**（見 §3 US-007 redirect note）— **不列入** Phase 4 DoD
   - US-008 per-QueryType 觀測指標：M2 WS-3 (`parallax_shadow_*`) + M3 §6 (`parallax_dual_read_*` / `parallax_arbitration_*` / `parallax_aphelion_unreachable_rate` / `parallax_crosswalk_miss_rate`) gauges 已 cover；per-QueryType cut **DEFERRED to v0.4**（見 §3 US-008 redirect note）— Phase 4 DoD 採 M2/M3 gauges 為觀測替代物
   - US-011 (Dual-read 路由器) + US-012 (欄位仲裁 engine) 為 **M3 milestone**，DoD 與驗收條件由 [`.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md`](../.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md) §6 管，**不列入** Phase 4 DoD
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
- Week 3: US-009 (a2a integration) — US-007/US-008 deferred (見 §3 + §5 carve-out)
- Week 4: US-010 (docs) + canary + 驗收

US-011/US-012 為 M3 milestone，timeline 由 [`.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md`](../.omc/plans/ralplan-m3-l2-dualread-2026-04-27.md) 管，不列入本 Phase 4 估計。

---

## 8. 一句話

**Parallax 是 state/provenance/timeline 的 SoT；Aphelion 是 semantic recall 的 SoT；衝突時做欄位級仲裁並寫成 event，為 Engram 升級累積 migration 的 golden dataset。**
