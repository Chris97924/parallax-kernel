-- Parallax Kernel — P0 Canonical Schema (SSoT)
-- Phase 0 scope: memories + claims implemented (with content_hash UNIQUE dedup).
--                sources / decisions / events / index_state built as empty + index
--                to avoid P1 schema migration.
-- Append-only contract for events enforced in application layer (sqlite_store.py
-- exposes only insert_event(); DB-level trigger deferred to Phase 1).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1. sources ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
  source_id    TEXT PRIMARY KEY,   -- sha256(content) 前 16 hex + uri slug
  uri          TEXT NOT NULL,      -- file://... or https://...
  kind         TEXT NOT NULL,      -- file | url | chat | discord
  content_hash TEXT NOT NULL,      -- full sha256
  user_id      TEXT NOT NULL,
  ingested_at  TIMESTAMP NOT NULL,
  state        TEXT NOT NULL       -- ingested | parsed | archived
);
CREATE INDEX IF NOT EXISTS idx_sources_user ON sources(user_id);

-- 2. memories (Phase 0 implemented) -------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
  memory_id    TEXT PRIMARY KEY,   -- ulid
  user_id      TEXT NOT NULL,
  source_id    TEXT REFERENCES sources(source_id),
  vault_path   TEXT NOT NULL,      -- users/{uid}/memories/xxx.md
  title        TEXT,
  summary      TEXT,
  content_hash TEXT NOT NULL,      -- sha256(normalize(title||summary||vault_path))
  state        TEXT NOT NULL,      -- draft | active | archived
  created_at   TIMESTAMP NOT NULL,
  updated_at   TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_user_state ON memories(user_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_memories_content ON memories(content_hash, user_id);

-- 3. claims (Phase 0 implemented) ---------------------------------------------
CREATE TABLE IF NOT EXISTS claims (
  claim_id     TEXT PRIMARY KEY,   -- ulid
  user_id      TEXT NOT NULL,
  subject      TEXT NOT NULL,
  predicate    TEXT NOT NULL,
  object       TEXT NOT NULL,
  source_id    TEXT NOT NULL REFERENCES sources(source_id),  -- closes UNIQUE NULL-hole; direct input uses synthetic 'direct:<user_id>' source
  content_hash TEXT NOT NULL,      -- sha256(normalize(subject||predicate||object||source_id||user_id)); ADR-005, v0.5.0-pre1
  confidence   REAL,
  state        TEXT NOT NULL,      -- auto | pending | confirmed | rejected
  created_at   TIMESTAMP NOT NULL,
  updated_at   TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_user_state ON claims(user_id, state);
CREATE INDEX IF NOT EXISTS idx_claims_subject    ON claims(user_id, subject);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_content ON claims(content_hash, source_id, user_id);

-- 4. decisions (empty skeleton) ------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
  decision_id    TEXT PRIMARY KEY, -- ulid
  user_id        TEXT NOT NULL,
  target_kind    TEXT NOT NULL CHECK (target_kind IN ('claim','memory','source')),
  target_id      TEXT NOT NULL,
  action         TEXT NOT NULL,    -- confirm | reject | archive | revoke
  actor          TEXT NOT NULL,    -- user | system | rule:<rule_name>
  approval_tier  TEXT,             -- P0 預留欄位，P1 實作
  state          TEXT NOT NULL,    -- proposed | approved | applied | revoked
  rationale      TEXT,
  created_at     TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_target ON decisions(target_kind, target_id);

-- 5. events (empty skeleton; append-only enforced in app layer) ----------------
-- session_id (nullable) added in migration 0006 for Claude Code session
-- continuity; included here so direct schema bootstraps pick it up too.
CREATE TABLE IF NOT EXISTS events (
  event_id       TEXT PRIMARY KEY, -- ulid
  user_id        TEXT NOT NULL,
  actor          TEXT NOT NULL,    -- user | system | worker:<name>
  event_type     TEXT NOT NULL,    -- claim.state_changed | memory.created ...
  target_kind    TEXT,
  target_id      TEXT,
  payload_json   TEXT NOT NULL,    -- before/after snapshot
  approval_tier  TEXT,             -- P0 預留
  created_at     TIMESTAMP NOT NULL,
  session_id     TEXT              -- v0.3.0 session continuity dimension
);
CREATE INDEX IF NOT EXISTS idx_events_target       ON events(target_kind, target_id);
CREATE INDEX IF NOT EXISTS idx_events_type_time    ON events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_events_user_time    ON events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_session      ON events(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type_session ON events(event_type, session_id);

-- 6. index_state (empty skeleton) ---------------------------------------------
CREATE TABLE IF NOT EXISTS index_state (
  index_name       TEXT NOT NULL,   -- chroma | memvid | graph_cognee
  version          INTEGER NOT NULL,
  last_built_at    TIMESTAMP,
  source_watermark TEXT,            -- last synced event_id (rebuild replay point)
  doc_count        INTEGER,
  state            TEXT NOT NULL,   -- building | ready | stale | rebuilding
  error_text       TEXT,
  PRIMARY KEY (index_name, version)
);
