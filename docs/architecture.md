# Parallax Architecture

This document describes how the Parallax kernel is structured, how data flows through the three layers, and what contracts are frozen into schema, migrations, and ADRs. For the full reasoning behind each design choice see [`docs/adr/`](adr/index.md).

---

## Overview

Parallax is a **content-addressed canonical knowledge-base store** built on SQLite. It stores everything a developer assistant needs to remember across sessions: file edits, decisions, extracted claims, and structured memories. The design priority is correctness first (append-only events, deterministic dedup, explicit state machines), then retrieval quality (six-intent router, progressive disclosure), then multi-user scale (v0.6 hub-and-spoke HTTP server).

The Python package is `parallax`; the PyPI / GitHub name is `parallax-kernel`.

---

## Three-Layer Data Flow

Data enters as raw events, is compressed into memories and claims, and is exposed through a typed retrieval API.

```
┌─────────────────────────────────────────────────────────────┐
│  CAPTURE                                                      │
│  Hook fires (SessionStart, PreToolUse, PostToolUse, ...)      │
│  → parallax.hooks  →  events table  (append-only)            │
└──────────────────────────────┬──────────────────────────────┘
                               │ events are immutable; triggers
                               │ enforce no UPDATE / DELETE
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  COMPRESS                                                     │
│  Extractors read events, produce memories + claims            │
│  → parallax.ingest  →  memories / claims  (content-hash dedup)│
│  → parallax.extract (shadow writer, [extract] extra)          │
└──────────────────────────────┬──────────────────────────────┘
                               │ idempotent UPSERT; same
                               │ content → same row on any machine
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  RETRIEVE                                                     │
│  Explicit retrieval ops  →  RetrievalHit  (L1/L2/L3)         │
│  → parallax.retrieve / parallax.retrieval  →  caller          │
└─────────────────────────────────────────────────────────────┘
```

The three layers are deliberately one-directional. Capture writes only to `events`. Compress reads `events` and writes `memories`/`claims`. Retrieve reads everything but writes nothing. This makes replay (`parallax.replay`) a first-class operation: `replay_events(conn)` can rebuild the entire compressed layer from the append-only event log.

---

## 6-Object Schema

```
┌──────────┐        ┌──────────────┐        ┌──────────────┐
│  sources │◄───────│   memories   │        │    claims    │
│          │        │              │        │              │
│ source_id│        │ memory_id    │        │ claim_id     │
│ name     │        │ title        │        │ subject      │
│content_  │        │ summary      │        │ predicate    │
│  hash    │        │ vault_path   │        │ object       │
└──────────┘        │ state        │        │ source_id ──►│──► sources
     ▲              │content_hash  │        │ user_id      │
     │              └──────────────┘        │ state        │
     │                                      │content_hash  │
     │              ┌──────────────┐        └──────────────┘
     │              │  decisions   │
     │              │              │
     └──────────────│ target_kind  │        ┌──────────────┐
                    │ target_id    │        │    events    │
                    │ rationale    │        │  (audit log) │
                    └──────────────┘        │ event_id     │
                                            │ event_type   │
                    ┌──────────────┐        │ target_kind  │
                    │ index_state  │        │ target_id    │
                    │              │        │ payload_json │
                    │ index_name   │        │ session_id   │
                    │ doc_count    │        └──────────────┘
                    │ state        │
                    │ source_wmark │
                    └──────────────┘
```

Key invariants:

- `events` is **append-only**: DB triggers (`events_no_update`, `events_no_delete`) abort any UPDATE or DELETE.
- `memories.content_hash` and `claims.content_hash` are the dedup keys; re-ingesting identical content converges to the same row on any machine (ADR-001).
- `claims.content_hash` includes `source_id` and `user_id` so two users sharing a source each own their own rows (ADR-004, ADR-005).
- `decisions.target_kind` is hard-CHECK'd to `{memory, claim, source}`; `events.target_kind` is unconstrained so the audit log can record decision-level state changes (ADR-003).

---

## Retrieval API Surface

Six typed entry points replace a free-form query surface. Each returns `RetrievalHit` objects with an L1/L2/L3 progressive-disclosure projection.

| Op | Purpose |
|---|---|
| `recent_context` | Last N sessions' memories and claims, recency-ranked. |
| `by_file` | Claims and events linked to a vault path (LIKE-escaped). |
| `by_decision` | Decision rows and their associated claims, batched. |
| `by_bug_fix` | Claims matching bug-fix predicates, OR-joined in one query. |
| `by_timeline` | Events and claims in an ISO-8601 window (microsecond-inclusive). |
| `by_entity` | Exact subject match on claims plus indexed neighbours. |

**L1/L2/L3 progressive disclosure:**

- **L1** — headline ≤ 120 chars; used by the `SessionStart` injector.
- **L2** — context row ~400 chars; suitable for inline context injection.
- **L3** — full row dict with all columns; used by `--explain` CLI trace.

The injector (`parallax/injector.py`) renders an L1 `<system-reminder>` block capped at `MAX_REMINDER_CHARS = 2000` and injects it at `SessionStart`.

### Intent Router (v0.5.x / ADR-006)

A retrieval-filtered pipeline classifies each question into one of six closed intents (`temporal`, `multi_session`, `preference`, `user_fact`, `knowledge_update`, `fallback`) via a two-layer gate (rule → Gemini Flash → fallback). Each intent maps to a specialised retriever; all fall back to MMR top-32 when the retriever returns fewer than `K_MIN = 3` hits. See [ADR-006](adr/index.md) for the full contract and evaluation protocol.

---

## Hub-and-Spoke HTTP Server (v0.6)

In v0.6, Parallax ships a FastAPI hub so many clients can share one canonical kernel over the network.

```
┌───────────────────────────────────────────┐
│  parallax serve  (FastAPI hub)            │
│  PARALLAX_TOKEN=<secret> bearer auth      │
│  GET /healthz   POST /ingest/*  GET /query │
└───────────┬────────────────┬──────────────┘
            │                │
  ┌─────────▼──┐     ┌───────▼────────┐
  │ Claude Code│     │  Other client  │
  │ Session-   │     │  (script, CI,  │
  │ Start hook │     │   web viewer)  │
  └────────────┘     └────────────────┘
```

**Client configuration:** set `PARALLAX_API_URL=http://<host>:<port>` and `PARALLAX_TOKEN=<secret>` in the client environment. The `plugins/parallax-session-hook/` plugin is the reference `SessionStart` hook implementation.

**Start the server:**

```bash
pip install -e '.[server]'
parallax serve --host 127.0.0.1 --port 8765
```

See [Deploy](deploy.md) for Fly.io, Railway, and Docker deployment. See [TLS](tls.md) for Caddy and Cloudflare Tunnel TLS setup.

---

## Content-Addressed Identity (ADRs)

The key design decisions are frozen in six ADRs under [`docs/adr/`](adr/index.md):

| ADR | Decision |
|---|---|
| ADR-001 | `content_hash = sha256(NFC-strip + "||" join)` — one algorithm, one module, no hand-rolling. |
| ADR-002 | WAL mode + page-1 corruption detection policy — accepted two outcomes: DatabaseError or clean recovery. |
| ADR-003 | `events.target_kind` intentionally unconstrained; `decisions.target_kind` hard-CHECK'd to `{memory,claim,source}`. |
| ADR-004 | `claims.content_hash` includes `source_id` — identical triple under different sources is two rows. |
| ADR-005 | `claims.content_hash` also includes `user_id` — supersedes ADR-004's 4-part formula with a 5-part formula. |
| ADR-006 | Retrieval-filtered pipeline: six-intent closed set, two-layer router, MMR fallback floor. |

---

## Testing Discipline

- **Append-only triggers** — `tests/test_migrations.py::TestAtomicityFix01` proves DDL + ledger insert are atomic; no shipped migration calls `executescript`.
- **Migration contracts** — every migration has a `down()` path; `migrate_down_to(n)` is regression-tested.
- **80% coverage floor** — enforced in `pyproject.toml` via `--cov-fail-under=80`; CI fails below this threshold.
- **Stress suite** — `tests/stress/` covers concurrent UPSERT, fault injection (page-1 corruption, mid-ingest kill), and Hypothesis property fuzz over the hashing boundary.
- **SQL acceptance harness** — `tests/acceptance/` proves the 4 Phase-2 acceptance questions at the DB layer (canonical existence, identity, state traceability, rebuild stability).
