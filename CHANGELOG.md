# Changelog

All notable changes to this project are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.4] - 2026-04-18

### Added
- `parallax/migrations/` — schema migration framework. `Migration` frozen
  dataclass (`version`, `name`, `up`, `down`), `MIGRATIONS` registry,
  `migrate_to_latest(conn)`, `migrate_down_to(conn, target_version)`,
  `applied_versions(conn)`, `pending(conn)`. Each migration runs in its
  own transaction with the `schema_migrations` ledger insert; failure
  rolls both back. Three migrations ship in v0.1.4:
  - **0001 initial_schema** — the historical `schema.sql` baseline.
    Idempotent (`CREATE IF NOT EXISTS` everywhere) so already-bootstrapped
    DBs upgrade without duplicate-table errors.
  - **0002 events_append_only** — installs `events_no_update` and
    `events_no_delete` BEFORE-UPDATE / BEFORE-DELETE triggers that
    `RAISE(ABORT, 'events are append-only')`. Promotes the append-only
    contract from app-layer convention to a DB-level guarantee.
  - **0003 claim_metadata** — adds the `claim_metadata` sidecar table
    (`claim_id PK -> claims.claim_id`, `reaffirm_count`, `last_seen_at`,
    `superseded_by`, `superseded_at`, `created_at`, `updated_at`) +
    `idx_claim_metadata_superseded_by`. Lets reaffirm/state-change
    writers update a narrow row instead of touching the dedup-key
    surface on `claims`.
- `parallax.events` — `record_event(conn, *, user_id, actor, event_type,
  target_kind, target_id, payload, approval_tier=None) -> str` writes a
  ULID-keyed audit row, validates the `(target_kind, target_id)` pair
  via `target_ref_exists` (orphan rejection raises `ValueError`), and
  json-serializes the payload. Convenience helpers
  `record_memory_reaffirmed` and `record_claim_state_changed` cover the
  two event types that the ingest / state-machine layers emit today.
- `parallax.transitions` — machine-readable mirror of
  `docs/state-transitions.md`. `MEMORY_TRANSITIONS`, `CLAIM_TRANSITIONS`,
  `SOURCE_TRANSITIONS`, `DECISION_TRANSITIONS` as
  `dict[str, frozenset[str]]`; `is_allowed_transition(entity, from, to)`
  validator. Terminal states map to `frozenset()`.
- `parallax.index.rebuild_index(conn, index_name)` — minimal Phase-1
  replay path. Recomputes a fresh `index_state` row from the live
  `memories` + `claims` rows in the active state, bumps version
  monotonically per `index_name`, sets `state='ready'`,
  `source_watermark = last event_id`. Full per-event replay deferred
  to Phase 5.
- `tests/test_migrations.py`, `tests/test_events.py`,
  `tests/test_transitions.py`, `tests/test_index.py` — new regression
  suites covering migration framework, append-only triggers,
  claim_metadata schema, event helpers, ingest reaffirmed wiring,
  state matrices, and rebuild_index.

### Changed
- `bootstrap.py` no longer executes `schema.sql` directly. The bootstrap
  path now opens the DB via `parallax.sqlite_store.connect` and calls
  `parallax.migrations.migrate_to_latest(conn)`. The `--schema` argument
  is preserved on `ParallaxConfig` for backward compatibility but is no
  longer the apply path; migrations under `parallax.migrations` own the
  DDL.
- `tests/conftest.py::conn` fixture now applies migrations instead of
  executing `schema.sql`, so unit tests run against the same DDL path
  bootstrap uses (including the events triggers and claim_metadata
  table).
- `parallax.ingest.ingest_memory` emits exactly one `memory.reaffirmed`
  event per dedup hit by calling `record_memory_reaffirmed` after the
  dedup counter increment. First-insert paths emit zero events.
- `parallax/__init__.py` re-exports `record_event`,
  `record_memory_reaffirmed`, `record_claim_state_changed`,
  `is_allowed_transition`, the four `*_TRANSITIONS` matrices, and
  `rebuild_index`.
- `parallax.introspection.ParallaxInfo.schema_version` typed as
  `int | None` (was `str | None`); the value comes straight from
  `schema_migrations.version` which is `INTEGER`.

### Packaging
- `pyproject.toml` version bumped to 0.1.4; `parallax.__version__` matches.

## [0.1.3] - 2026-04-18

### Fixed
- `schema.sql` adds a hard `CHECK(target_kind IN ('claim','memory','source'))`
  on `decisions.target_kind`, matching the comment-only constraint that
  shipped in v0.1.2 and closing the gap the 2-agent review flagged.
  `events.target_kind` stays unconstrained by design so the audit log can
  record decision-level state changes.
- `tests/stress/test_fault_injection.py::TestCorruptDB` now overwrites
  256 bytes INSIDE page 1 (offset 100, after the 100-byte SQLite header
  and inside the B-tree) instead of appending garbage after the last
  page — SQLite sizes the database from the page count in the header,
  not from `stat()`, so the v0.1.2 trailing-append was a silent no-op
  that never detected corruption.
- `tests/stress/test_fuzz_ingest.py::TestDistinctInputsFuzz` swaps the
  bare `return` on NFC-degenerate inputs for `hypothesis.assume(...)`
  so Hypothesis stops spending `max_examples` budget on uninteresting
  same-canonical-input cases. Effective coverage of the distinct-input
  space is now proportional to the configured example count.
- `tests/stress/test_fault_injection.py::TestCorruptDB` regression-test
  method renamed from `test_appending_junk_to_wal_is_not_silent` to
  `test_page1_corruption_is_not_silent` (name now matches the v0.1.3
  behavior); class docstring and inline `PASSIVE checkpoint` comment
  updated to describe the TRUNCATE-then-page-1 flow.

### Added
- `parallax.validators.DECISION_TARGET_KINDS` — narrower frozenset
  ({memory, claim, source}) matching the `decisions.target_kind` CHECK,
  exposed alongside the existing events-wide `VALID_TARGET_KINDS` and a
  new `TargetKind = Literal["memory","claim","source","decision"]`
  alias. Re-exported from the package root.
- `parallax.validators.target_ref_exists` docstring now documents the
  transaction-isolation requirement: callers must hold a single
  transaction spanning check → dependent insert under WAL-mode SQLite
  to avoid TOCTOU races.
- `tests/test_schema.py` — new DB-level regression suite backing
  ADR-003: asserts the `decisions.target_kind` CHECK rejects
  `'decision'` and `'foo'`, accepts `{'claim','memory','source'}`, and
  that `events.target_kind='decision'` still inserts cleanly
  (audit-log contract).
- `docs/adr/` — ADR backfill for v0.1.3 design decisions: ADR-001
  (`content_hash` SSoT), ADR-002 (WAL + page-1 corruption detection
  policy), ADR-003 (`events.target_kind` vs `decisions.target_kind`
  asymmetry), plus index/template at `docs/adr/README.md`.

### Changed
- `docs/state-transitions.md` — terminal-state self-cells
  (`archived → archived`, `rejected → rejected`, `revoked → revoked`)
  now render as `-` with a footnote explaining why self-entry is a
  no-op. Cross-entity invariants section notes that
  `decisions.target_kind` is narrower than `events.target_kind`. Schema
  line refs updated to point at the state-vocabulary lines
  (`schema.sql:32` for memories, `:49` for claims, `:19` for sources,
  `:66` for decisions).
- `tests/stress/REPORT.md` — adds a GIL caveat on the concurrency
  suite (Python threads stress SQLite-level locking, not
  multi-OS-process behavior; see `TestMidIngestKill` for cross-process
  coverage) and documents the three v0.1.3 patches.

### Packaging
- `pyproject.toml` version bumped to 0.1.3.

## [0.1.2] - 2026-04-18

### Added
- `parallax.validators.target_ref_exists(conn, target_kind, target_id)`
  stub: returns True/False for `(kind, id)` pairs drawn from
  `{memory, claim, source, decision}`; raises `ValueError` on unknown
  kinds. Re-exported from the package root alongside
  `VALID_TARGET_KINDS`. No caller wires it yet — the stub pins the
  public surface for forthcoming event / decision writers.
- `tests/stress/` suite:
  - `test_fuzz_ingest.py` — hypothesis property fuzz (idempotence, NFC
    collapse, distinct-input → distinct hash, schema formula, long
    strings up to 10_000 chars, boundary int user-id suffixes). 320
    generated examples per run.
  - `test_concurrent_upsert.py` — 10 threads × 100 iter on identical
    content_hash collapses to one row with zero phantom ids; mixed-
    content variant proves zero lost UPSERTs across 50 distinct rows.
  - `test_fault_injection.py` — mid-ingest subprocess kill preserves
    WAL-committed rows; appended-garbage corruption either recovers or
    raises `sqlite3.DatabaseError` (no silent corruption);
    `PRAGMA wal_checkpoint(TRUNCATE)` round-trip shrinks WAL and
    preserves 50 committed rows.
  - `tests/stress/REPORT.md` — run summary, reproduction commands,
    findings.
- `docs/state-transitions.md` — state-transition matrix for memories,
  claims, sources, decisions (rows=from, cols=to, cells=allowed +
  trigger). Includes cross-entity invariants (target_ref_exists gate,
  terminal states, state-change event emission).
- `hypothesis>=6` added to `[project.optional-dependencies].dev`.

### Changed
- **Breaking (internal contract):** `parallax.hashing.normalize(*parts)`
  now raises `TypeError` when any part is `None`. Callers holding
  `Optional[str]` values must convert to `""` themselves. Makes the
  boundary contract explicit rather than silently collapsing `None` and
  `""` to the same digest. `parallax.ingest.ingest_memory` converts
  `title=None` / `summary=None` to `""` before hashing; public
  ingest-function signatures are unchanged and existing callers see no
  behavior change.

### Packaging
- `pyproject.toml` version bumped to 0.1.2.

## [0.1.1] - 2026-04-17

### Added
- `parallax/__init__.py` re-exports the public API so callers can
  `from parallax import ingest_memory, ingest_claim, memories_by_user,
  claims_by_user, claims_by_subject, memory_by_content_hash,
  claim_by_content_hash, Source, Memory, Claim, Event`. Module now also
  exposes `__version__`.
- `examples/quickstart.py` — a 30-line bootstrap → ingest → retrieve demo
  that exercises only the public surface.
- `.github/workflows/tests.yml` — GitHub Actions CI on Python 3.11 running
  `pytest` on every PR and push to `main`.

### Fixed
- `ingest.ingest_memory` / `ingest.ingest_claim` are now race-safe against
  concurrent duplicate writes: the previous SELECT-then-INSERT pattern
  could return a ULID that a concurrent `INSERT OR IGNORE` silently
  dropped. The implementation now `INSERT OR IGNORE`s then re-SELECTs by
  the UNIQUE `(content_hash, user_id)` / `(content_hash, source_id)`
  index, so callers always receive the persisted winner's id.

### Packaging
- Project renamed from `parallax` to `parallax-kernel` on PyPI / GitHub;
  the Python import name stays `parallax`.
- `.gitignore` excludes internal dev-loop artifacts (`prd.json`,
  `progress.txt`, `docs/cloud.md`, `.omc/`) so the OSS tree stays clean.

## [0.1.0] - 2026-04-17

### Added
- `parallax.hashing` — `normalize(*parts)` and `content_hash(*parts)`, the SSoT
  canonicalizer + sha256 hasher. NFC, strip, `||` separator.
- `parallax.config` — frozen `ParallaxConfig` + `load_config()` (env vars with
  project-root defaults; optional `.env` via python-dotenv).
- `parallax.sqlite_store` — `insert_source / insert_memory / insert_claim /
  insert_event / query / reaffirm` plus `Source / Memory / Claim / Event`
  dataclasses. Events are write-only by export whitelist.
- `parallax.ingest` — `ingest_memory` / `ingest_claim` with UPSERT semantics
  and a lazily-created synthetic `direct:<user_id>` source.
- `parallax.retrieve` — `memories_by_user`, `claims_by_user`,
  `claims_by_subject`, `memory_by_content_hash`, `claim_by_content_hash`.
- `bootstrap.py` — one-shot initializer + `python bootstrap.py <path>` CLI.
- Project scaffolding: `pyproject.toml`, `.gitignore`, `.env.example`,
  `README.md`, `CHANGELOG.md`, `LICENSE`.
