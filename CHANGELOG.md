# Changelog

All notable changes to this project are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **`POST /event` route.** Receives Orbit's M6 dual-write envelope and persists
  it to the existing `events` table as a system-level audit row
  (`target_kind=None`) via `parallax.events.record_event`. Authenticated via
  the existing bearer token (`require_auth`). No new table, no new migration —
  reuses `events` schema added in `m0001_initial_schema.py:80-90`. Closes
  Orbit M6 rollout step 0 path-(a) defer blocker. New schemas:
  `EventIngestRequest` / `EventIngestResponse` in
  `parallax/server/schemas.py`. Test surface: 7 integration cases in
  `tests/server/test_event_route.py` (auth/missing-field/empty-string/payload
  round-trip/extra-field-forbid) + 22 parametrised unit cases in
  `tests/test_event_ingest_schema.py`.
- **`parallax serve` CLI subcommand.** Closes the gap where the Dockerfile,
  Railway config, and other deploy scripts referenced `parallax serve --host
  ... --port ...` but no such subcommand existed. The new subcommand pins
  `PARALLAX_BIND_HOST` to the supplied `--host` *before* importing the app,
  so `assert_safe_to_start()` sees the real bind address. Operators who
  invoke uvicorn directly (e.g. `pm2/ecosystem.config.js`) must still set
  `PARALLAX_BIND_HOST` themselves to match — `pm2/ecosystem.config.js` now
  documents and sets this explicitly. Two new tests in
  `tests/server/test_server_safety.py::TestServeCliPinsBindHost` pin the
  pin: one checks the auto-set, one checks `setdefault` semantics so an
  operator-supplied env value is preserved.
- **Audit warnings for the two safety escape hatches.**
  - `PARALLAX_ALLOW_OPEN_PUBLIC=1` now logs `auth.startup.allow_open_public_override`
    at WARN with the bind host. Post-incident log readers can tell when the
    safety net was disabled.
  - `PARALLAX_METRICS_PUBLIC=1` now logs `auth.metrics.public_override_active`
    at WARN at app construction.
  Tests pin both messages indirectly via the existing safety matrix.

### Changed (post-review hardening)
- **`tests/server/test_server_safety.py::TestMetricsAuthPosture` now scrubs
  `PARALLAX_BIND_HOST` and `PARALLAX_ALLOW_OPEN_PUBLIC`.** A future test
  reordering or env leakage could otherwise cause `_make_app` to raise on
  an open-mode metrics test that would then look like a regression in
  the metrics route. Now independent of bind-host config.
- **New test pinning the `PARALLAX_METRICS_PUBLIC=1` semantics.**
  `test_metrics_public_override_accepts_anonymous_AND_wrong_token` verifies
  the override genuinely bypasses auth (a wrong token still gets 200), not
  just absorbs missing headers — closes a security-reviewer finding.
- **`docs/contract.md` covers the full `parallax/__init__.py` `__all__`
  surface plus the documented `replay_events` exception** to the atomic
  transition contract.
- **README "Server / Production safety" section rewritten** to make the
  env→bind decoupling explicit, list both launchers, and call out the
  two override audit warnings operators should grep for.

### Changed (initial pass)
- **`transition_claim_state()` — canonical atomic claim state-change API.**
  New helper in `parallax.events` (also re-exported from `parallax`)
  that wraps `SELECT current state → is_allowed_transition → UPDATE
  claims SET state, updated_at → record_event` in a single transaction
  with a TOCTOU rowcount guard. Closes the gap left by
  `record_claim_state_changed()`, which only writes the audit event and
  does NOT mutate `claims.state`. Optional `expected_user_id` argument
  enables a defensive cross-tenant guard for multi-user routes. Existing
  callers that already do their own UPDATE in the same transaction
  (e.g. `parallax.extract.review._transition`, which adds a stricter
  `from_state='pending'` rule) continue to use the lower-level
  `record_claim_state_changed` and are unaffected.
- **Production-safety guards for the FastAPI server.** Three new env
  vars and one boot-time refusal:
  - `PARALLAX_BIND_HOST` is read at app construction. When set to a
    non-loopback address while `PARALLAX_TOKEN` and `PARALLAX_MULTI_USER`
    are both unset, `assert_safe_to_start()` raises `RuntimeError` and
    the process refuses to come up. Override with
    `PARALLAX_ALLOW_OPEN_PUBLIC=1` (NOT recommended).
  - `PARALLAX_METRICS_PUBLIC=1` opts `/metrics` out of auth even when a
    token is configured (private network / Cloudflare Access scenarios).
- **`docs/contract.md`.** New table mapping every public API to
  whether it mutates storage, logs an event, or is read-only — closes
  the README-vs-implementation drift that had `record_claim_state_changed`
  documented as "applies a transition" while in fact only writing an
  event. Linked from `README.md` "State Machine".

### Changed
- **`memory_by_content_hash` and `claim_by_content_hash` require
  `user_id` (keyword-only, mandatory).** Previously these queried
  `WHERE content_hash = ?` only. Memories are stored with a unique
  index on `(content_hash, user_id)` and the hashing layer does NOT
  fold `user_id` into the memory hash — meaning the same content under
  two different users coexists with the same `content_hash`, and a
  hash-only lookup could return another tenant's row. Claim hashes are
  already user-scoped via ADR-005 (v0.5.0-pre1) so the risk there is
  mathematical not practical, but the API is symmetric for defence in
  depth. Calling without `user_id` now raises `TypeError` rather than
  silently leaking. Internal callers updated; the public-API surface
  pins `__all__` so `parallax/__init__.py` still re-exports both.
- **`/metrics` is auth-gated by default when a token is configured.**
  Previously the route was unconditionally unauthenticated. In open mode
  (no `PARALLAX_TOKEN`, no `PARALLAX_MULTI_USER`) it stays open to
  match `/healthz`; with auth configured it now requires the same
  bearer the rest of the API does, unless `PARALLAX_METRICS_PUBLIC=1`
  is set explicitly. Closes a reconnaissance vector where ingest
  cadence, retrieve volume, and shadow-discrepancy rate would leak to
  any unauthenticated caller on a public listener.
- **`record_claim_state_changed()` docstring clarifies the contract.**
  It writes the audit event only and does NOT mutate `claims.state`.
  The README "State Machine" example was corrected (the old
  `("pending", "confirmed") in CLAIM_TRANSITIONS` snippet was buggy
  because `CLAIM_TRANSITIONS` is `dict[str, frozenset[str]]`, not a
  set of tuples) and the section now points readers at
  `transition_claim_state()` for the mutation path.
- **`rebuild_index()` README/docstring honest about idempotency.** It
  is *deterministic in derived content* — `doc_count`, `state`, and
  `source_watermark` stay stable on repeat calls — but it is *not
  DB-idempotent*: each call appends a new `index_state` row at
  `version = MAX(version) + 1`. The history is intentional. Acceptance
  harness `04_rebuild_identical.sql` already permits the version bump.
- **CI now triggers on push to `main-next`.** `tests.yml` previously
  ran on push to `main` only; since `main-next` has been the default
  branch, direct commits there were skipping the test job. Push trigger
  now lists `[main, main-next]`; PR trigger remains all-branch.
- **README version table no longer claims `__version__ = "0.5.0"`.**
  `pyproject.toml` and `parallax/__init__.py` were already at `0.6.0`;
  this was a stale documentation cell. Telemetry section also rewritten
  — `parallax.telemetry` remains stdlib-only, but `prometheus_client`
  was already a core dep (used by the `/metrics` HTTP adapter), and the
  README now reflects that boundary instead of the old "Prometheus
  intentionally out of scope" line.

### Changed (pre-existing entry)
- **Migration m0012 — `crosswalk.dpkg_doc_id` renamed to `aphelion_doc_id`.**
  Completes the DPKG → Aphelion rebrand on the Parallax side (Aphelion v0.4.0
  shipped 2026-04-24 with `aphelion_spec_version` wire break). Uses
  `ALTER TABLE ... RENAME COLUMN` so existing rows (if any) are preserved.
  Down-migration restores the old name. m0012 also reconciles the on-disk
  schema with `docs/phase4-dual-memory-prd.md` US-001, which has always
  specified `aphelion_doc_id` — m0011 shipped under the legacy name and
  m0012 closes that gap. No runtime code referenced the old column name
  (`parallax/router/backfill.py` crosswalk upserts omit the doc-id column
  entirely; no other reader exists), so the rename is transparent to
  existing Lane D-1/D-2 code. `up()` / `down()` both guard for
  `sqlite3.sqlite_version_info >= (3, 25, 0)` and raise a clear
  `RuntimeError` on older libsqlite3 rather than a cryptic
  `OperationalError`. Rollback: `migrate_down_to(target_version=11)`.

## [0.6.0] - 2026-04-22

Pre-release tag: `v0.6.0-pre1`. GA (`v0.6.0`) will land after a 7-day
dry-run observation window for Lane C Phase 1.

### Added
- **v0.6 Phase A — FastAPI server (localhost MVP).** `parallax/server/`
  exposes `/ingest`, `/query`, `/query/reminder`, `/inspect/health`,
  `/inspect/info`, and an unauthenticated `/healthz`. Single-token
  bearer auth via `PARALLAX_TOKEN`; opt-out warning when the env var
  is unset (localhost dev only). `parallax serve` CLI subcommand.
  `plugins/parallax-session-hook/` is a Claude Code SessionStart hook
  that renders `/query/reminder` into an injected `<system-reminder>`.
- **v0.6 Phase B — deploy + multi-user + viewer + cloud backup.**
  `deploy/{fly.toml,railway.json,Caddyfile,cloudflared.yml}` templates;
  `docs/{install,deploy,tls,architecture,adr/index}.md` + `mkdocs.yml`
  material theme. Migration **m0009** adds `api_tokens` (sha256 hash
  PK + user_id binding + revoked_at audit). `PARALLAX_MULTI_USER=1`
  unlocks per-user bearer auth; `parallax token create/list/revoke`
  CLI. `parallax/server/viewer.py` serves a 3-tab web UI behind
  `PARALLAX_VIEWER_ENABLED`. `parallax backup --to s3://...` and
  `restore --from s3://...` via `[cloud]` optional extra (boto3).
  `[server]` extra pins fastapi + uvicorn. README + CONTRIBUTING +
  ARCHITECTURE finalized; `mkdocs build --strict` green.
- **Lane C Phase 1 — MEMORY.md interception via Parallax (dry-run).**
  Migration **m0010** adds `memory_cards` with `UNIQUE(user_id, filename)`
  and a `category` CHECK constraint. `parallax/memory_md.py` parses
  Chris's 4-section `MEMORY.md` + YAML-frontmatter companions into
  frozen dataclasses (`MemoryMdEntry`, `CompanionFile`, `IngestReport`)
  and upserts through `_manual_tx` (single BEGIN IMMEDIATE / COMMIT /
  ROLLBACK). `parallax/server/routes/export.py` exposes
  `GET /export/memory_md` (auth-gated, deterministic section ordering,
  body-only belt-and-braces privacy filter). `docs/lane-c-phase1.md`
  is the operator guide with the Phase 2 switchover contract.

### Changed
- **Privacy filter now body-only + regex pattern.** The v0.5-era
  substring-based `contains_secret` is preserved for back-compat, but
  ingest + export paths now use `body_looks_like_secret`, a compiled
  regex that requires a secret-keyword + separator + 8+ high-entropy
  character value. Eliminates prior false positives against Chris's
  own MEMORY.md (e.g. the literal text `token 月度 cap lesson` no
  longer trips the filter). Names and descriptions are no longer
  scanned — they cannot plausibly carry secrets.
- **Ingest is now transactional end-to-end.** `ingest_memory_md` wraps
  the entire entry loop in one manual transaction. The old per-row
  `conn.commit()` is gone; a mid-loop exception rolls the whole batch
  back and re-raises. Pre-check SELECT + UPSERT for a single row live
  inside the same transaction so a concurrent writer can no longer
  mis-classify an insert as an update.

### Security
- **Companion-file path-traversal guard (F1).** `ingest_memory_md`
  validates `companion_path.resolve().relative_to(companion_dir.resolve())`
  before `.exists()` so a crafted filename with parent-escape tokens
  cannot even probe for file existence, let alone ingest arbitrary
  files. Failed entries land in `skipped_malformed`.
- **HTTP server hardening (8 reviewer findings + 1 bonus).** DB path
  in `/inspect` responses is now the filename only (no absolute host
  path); sqlite exception handlers return a generic message (no schema
  leak); SessionStart hook `_is_safe_url()` blocks `file://`, `ftp://`,
  `gopher://` so a poisoned `PARALLAX_API_URL` can't exfiltrate the
  Bearer token; `vault_path` field validator rejects `..`, absolute
  paths, drive letters, and NUL; OpenAPI docs (`/docs`, `/redoc`,
  `/openapi.json`) gated behind `PARALLAX_DOCS_ENABLED=1`.

### Fixed
- Pydantic + tenacity declared as runtime dependencies (were
  transitively imported but not listed in `[project.dependencies]`,
  causing fresh-clone `pytest` collection failures under ADR-006
  Day-0 scaffold).

### Tests
- 66 new Lane C tests (schema, ingest, privacy v2, path-traversal,
  atomicity, export, regenerate-atomic) plus the Phase A+B server
  and auth suites. Full suite: 421 passed, coverage 87.37% (gate 80%).
  `ruff check` clean on all new modules.

### Notes for Phase 2 switchover (must-do before flipping `.preview -> live`)
- **S1**: `memory_cards_metadata` migration + `POST /ingest/memory_md`
  endpoint + hook-side sha256 staleness detection + auto re-ingest.
  Without this, Phase 2 would overwrite fresh auto-memory edits with
  stale ingested data.
- **S3**: Preview file metadata comment (`<!-- parallax-export: ... -->`)
  + skip-write-if-unchanged in `regenerate.py`. Keeps `diff.log`
  signal-to-noise high through the 7-day observation window.

## [0.5.0] - 2026-04-20

### Added
- **LongMemEval benchmark harness** (`eval/longmemeval/`). Parallax's
  retrieval pipeline evaluated against the 500-question standardized
  benchmark. Shipped results: `s_baseline` 88.92% (297/334 CORRECT with
  Gemini-2.5-pro judge), `oracle_full` 86.96% (retrieval-free ceiling),
  `_s` split 86.0% on the 500Q cut. Harness includes pipeline runner,
  per-type breakdown, Pro-judge + flash-judge paths, rejudge tool, and
  `--explain` retrieval trace view for debugging.
- **ADR-006 Day-0 scaffolding — retrieval-filtered answerer pipeline.**
  `parallax.llm.call` with tenacity-backed retry + SQLite WAL cache
  (`busy_timeout=5000`, fallback-isolated hash); `parallax.retrieval`
  INTENT_PRIORITY contracts + MMR + embedding cache keyed on
  `(user_id, max_created_at)`; `parallax.answer.evidence` with sha256
  content-addressed cache keys; eval shims `gemini`, `ablate_fallback`,
  `sweep_thresholds`, and `schema_v2` (Pydantic v2 gate). 459 tests
  green (455 non-smoke + 4 smoke). critic(opus) APPROVED.
- **Migration m0008 — canonical timestamp normalization.** Normalizes
  all `TIMESTAMP` columns across the corpus to the 32-char canonical
  ISO-8601 form (`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`). Permanently
  closes the naive-ts same-second lexical-compare hole that caused
  by_timeline boundary bugs.
- **Migration m0007 — claim `content_hash` scoped to `user_id`.**
  Backfills all rows to the new hash; enforces ADR-005's requirement
  that dedup is per-user. Two users asserting the same triple now
  get two rows by design.
- `parallax.retrieval --explain` — retrieval trace view for LongMemEval
  debugging (surfaces intent classification, per-retriever hits,
  MMR selection, final evidence set).
- `scripts/bootstrap_linux.sh` — idempotent one-shot Linux installer
  (clone → venv → `pip install -e .[dev]` → `bootstrap.py` →
  `.env` template). Each machine gets its own independent brain until
  v0.6 HTTP server ships.

### Fixed
- **BUG 1+4 — `by_timeline` microsecond boundary & naive-ts lex compare.**
  `by_timeline` dropped rows whose `created_at` equaled the end bound
  when microsecond precision differed. Naive timestamps lexically
  compared against timezone-aware strings caused ordering inversions.
- **BUG 2 — `by_entity` / `by_bug_fix` missing `ORDER BY` on claim SELECT.**
  Retrieval order was driven by SQLite rowid, producing non-deterministic
  output across inserts. Added explicit `ORDER BY created_at DESC, id DESC`.
- **BUG 3 — `content_hash` missing `user_id` scope (ADR-005).**
  Claims from different users collapsed into one row when triples
  matched. Fixed via m0007 backfill; `parallax.ingest.ingest_claim`
  now hashes `(subject, predicate, object, source_id, user_id)`.
- **CLI P0 stability.** cp950-safe stdout/stderr on Windows;
  outer-guard wraps pipe close / SIGINT / unexpected exception paths
  so `parallax inspect | head` no longer produces BrokenPipe tracebacks.
- **`pydantic>=2` + `tenacity>=8` added to runtime dependencies.**
  ADR-006 Day-0 scaffold imports both, but `[project.dependencies]`
  only listed `python-ulid`, `python-dotenv`, `typing-extensions`.
  Clean-clone bootstrap + pytest collection failed without them.
- **LongMemEval `parse_verdict` silent-fail.** Pro-judge runs with
  thinking-token budget exhaustion returned empty text, which the old
  parser defaulted to INCORRECT. Now raises `ValueError`; `run_one`
  catches and emits `verdict=ERROR` so rejudge is possible post-hoc.

### Coverage
- 421 tests / 89.37% (v0.5.0 GA); 459 tests / comparable coverage after
  ADR-006 Day-0 merge.

## [0.4.0] - 2026-04-19

### Added
- **`parallax.replay` — full events-based rebuild of claims/memories.**
  `replay_events(conn, *, into_conn=None)` walks the events log in
  (created_at ASC, event_id ASC) order and applies
  `memory.created` / `claim.created` / `claim.state_changed` /
  `memory.state_changed` events to rebuild row state bit-for-bit. When
  `into_conn` is provided, rows are written into the target while events
  are read from the source — the production rebuild path against a
  schema-only DB. `*.state_changed` events carry `updated_at` in the
  payload so the column survives replay unchanged; events without
  `updated_at` (pre-0.4.0) fall back to a state-only UPDATE.
  Reaffirmation events are counted but do not mutate rows; unknown event
  types are skipped (not raised).
  `backfill_creation_events(conn)` is a one-shot helper for pre-0.4.0
  DBs: it synthesizes a `memory.created` / `claim.created` event for
  every row lacking one, preserving the row's own `created_at` so
  chronological order against coexisting `state_changed` events stays
  intact. Idempotent via NOT EXISTS guard.
- **`parallax.ingest` now emits creation events.** Every first-write
  `ingest_memory` / `ingest_claim` call records a `memory.created` /
  `claim.created` event carrying the full row payload — the events log
  is now the source of truth for row rebuild. Dedup hits continue to
  emit `*.reaffirmed`.
- **ADR-004: claim dedup includes `source_id`.** Codifies
  `claims.content_hash = sha256(normalize(subject||predicate||object||source_id))`
  as the dedup key. Two claims with identical triples but different
  sources remain two rows by design. Regression tests in
  `tests/test_claim_dedup_semantics.py`.
- **Migration dry-run / introspection.** `migration_plan(conn)` returns
  a frozen `MigrationPlan` (applied versions, pending `MigrationStep`s,
  current/target version). Each `MigrationStep` reports the DDL
  statements and a `row_impact_estimates` map of referenced tables →
  current row counts. `parallax inspect migrate [--dry-run] [--json]`
  prints the plan; the function is non-destructive (only `SELECT`s run).
- **`parallax.hashing.normalize` accepts `Optional[str]`.** `None` values
  are encoded with an internal sentinel (`\x00\x00NONE\x00\x00`) before
  `||`-join so `normalize(None)` and `normalize('')` produce different
  canonical strings. Closes the prior P1 LOW collision where
  `memory(title=None)` and `memory(title='')` hashed identically.
  Existing `title_for_hash = "" if title is None else title` shims
  removed from `ingest`.
- **`reaffirm()` typed signature.** `sqlite_store.reaffirm` changes from
  `*args, **kwargs -> None` to
  `(*, user_id, kind, entity_id, actor='system') -> str`. `kind` must
  be `"memory"` or `"claim"`; memory delegates to
  `record_memory_reaffirmed`, claim emits `claim.reaffirmed` via
  `record_event`. Returns the generated `event_id`.
- **Public API re-exports.** `parallax/__init__.py` now exports
  `content_hash`, `normalize`, `reaffirm`, `migrate_to_latest`,
  `migrate_down_to`, `migration_plan`, `MigrationPlan`,
  `MigrationStep`, `applied_versions`, `pending`, `replay_events`,
  `backfill_creation_events`, `ReplaySummary`, `BackfillSummary`.
  `tests/test_public_api.py` guards `__all__` against silent drift.

### Changed
- `__version__` bump 0.3.0 → 0.4.0.

## [0.3.0] - 2026-04-19

### Added
- **Session continuity minimum closure.** Five coordinated subsystems land
  together so a new Claude Code session can see last session's work:
  1. **`parallax/hooks.py` — Claude Code hook → events ingestion.**
     Maps `SessionStart`, `SessionEnd`/`Stop`, `UserPromptSubmit`,
     `PreToolUse` (Bash/Edit/Write/MultiEdit), and `PostToolUse`
     (Edit/Write/MultiEdit) hook fires onto `events` rows. File-edit
     hooks back-link to `memories.vault_path` via LIKE suffix match
     when tracked, and embed a `_path_sha16` fingerprint in the payload
     when not — so orphan file edits are still discoverable.
     `ingest_from_json()` takes a raw hook envelope so a single
     `jq | parallax inspect ingest` pipe works in CI.
  2. **`parallax/retrieve.py` — explicit retrieval API.** Six entry
     points (`recent_context`, `by_file`, `by_decision`, `by_bug_fix`,
     `by_timeline`, `by_entity`) replace the prior free-form query
     surface. Each returns `RetrievalHit` objects carrying an L1/L2/L3
     projection score, evidence snippet, and source ref.
  3. **3-layer progressive disclosure.** `RetrievalHit.project(level)`
     returns an L1 headline (≤120 chars), L2 context row (~400 chars),
     or L3 full row with `full` dict populated. Injector uses L1; CLI
     `--explain` uses L3.
  4. **`parallax inspect` CLI.** `parallax inspect events --session <id>`
     dumps hook-ingested events; `parallax inspect retrieve "<query>"
     --explain` runs the retrieval API and prints per-hit rationale
     (which column/keyword drove the score). `parallax inspect inject`
     prints the rendered `<system-reminder>` block for debugging.
  5. **`parallax/injector.py` — SessionStart injector.** Builds a
     length-capped (`MAX_REMINDER_CHARS = 2000`) `<system-reminder>`
     containing recently-modified files + last 3 decisions + recent
     context, with marker-safe truncation (`... (truncated)`).
- **`events.session_id` dimension** — migration 0006 adds nullable
  `session_id` to `events` plus two indexes (`idx_events_session`,
  `idx_events_type_session`) for session-scoped scans. Included in
  `schema.sql` so fresh bootstraps get the column directly.
- **`idx_events_user_time` index in `schema.sql`** — fresh bootstraps
  previously missed this (migration 0004 was the only source); now
  both paths produce identical index sets.

### Fixed
- **LIKE wildcard escaping.** `by_file`, `by_entity`, and
  `hooks._resolve_target_for_file` now escape `%`, `_`, and `\` in
  user-provided paths/subjects with `ESCAPE '\\'`. Previously a file
  named `utils_v2.py` would also match `utilsXv2.py`, and a subject
  `100% done` would match everything.
- **N+1 in `by_decision`.** Replaced per-hit claim lookup with a
  single `WHERE claim_id IN (…)` batch. Decision hit rendering is now
  O(1) DB round-trips regardless of result set size.
- **5× loop in `by_bug_fix`.** Replaced five sequential LIKE queries
  with a single OR-joined statement.
- **ISO-8601 variant equivalence in `by_timeline`.** `since`/`until`
  are normalized to UTC `isoformat()` before comparison so `'Z'`
  suffix and `'+00:00'` produce identical results against the TEXT
  `created_at` column. Out-of-order `since > until` now raises
  `ValueError` with a descriptive message instead of silently
  returning an empty window.
- **Injector `_trim_to_cap` mutation + marker corruption.** Now makes
  a defensive copy of the input list and reserves budget for the
  truncation marker so the output always terminates with the full
  `... (truncated)` suffix.
- **CLI import-time default-user trap.** `PARALLAX_USER_ID` is now
  resolved at command invocation via `_default_user()`, not at module
  import, so `monkeypatch.setenv` in tests and shell exports in ad
  hoc use are honored consistently.

### Tests
- 320 tests, 89.44% coverage. New suites:
  - `test_retrieve_api.py::TestLikeEscape` + `TestByTimelineErrors` —
    regression coverage for the wildcard + ISO-normalize fixes.
  - `test_hooks.py::TestIngestHookTools` — `Write` + `MultiEdit`
    branches of `_file_edit_event_type`.
  - `test_events_session_id.py`, `test_cli_inspect.py`,
    `test_injector.py` — new feature coverage.

## [0.2.1] - 2026-04-18

### Added
- **`parallax backup` / `parallax restore` CLI (Step 2 — low-risk, high ROI).**
  New top-level console script `parallax` with two subcommands:
  - `parallax backup <archive.tar.gz>` runs `PRAGMA wal_checkpoint(TRUNCATE)`
    on the live SQLite store BEFORE copying (stale WAL pages would otherwise
    leave the copied main-db file inconsistent), then writes a single
    `.tar.gz` containing `db/parallax.db`, `vault/**`, and a `manifest.json`
    (parallax_version, schema_version, created_at UTC, db_sha256, per-table
    row_counts, DISTINCT content_hash counts for memories + claims).
  - `parallax restore <archive.tar.gz> [--no-verify]` extracts into a
    temp dir, moves any existing db + vault aside as `<path>.bak-<UTC ts>`
    (never silently overwrites), installs the archive contents, and by
    default re-computes the manifest against the restored db and raises
    `RestoreVerificationError` on any drift (sha256 or row-count mismatch).
- **`parallax/backup.py` + `parallax/restore.py` + `parallax/cli.py`.**
  Zero non-stdlib dependencies: uses `tarfile` + `hashlib` + `argparse` +
  `sqlite3` only, so backup/restore work from a minimal install.
- **`[project.scripts]` entry point** — `parallax = "parallax.cli:main"`
  in `pyproject.toml` registers the `parallax` console script under
  `pip install -e .`.
- **`tests/test_backup_restore.py` — 9 tests.** Including the headline
  `test_backup_restore_round_trip_preserves_acceptance_sql`: seed a
  populated db + vault, back up, delete the db AND wipe the vault dir,
  restore, and assert the four Phase-2 acceptance SQL snapshots
  (`01_canonical.sql` / `02_identity.sql` / `03_state_traceable.sql` /
  `04_rebuild_identical.sql`) are byte-equivalent before and after.
  Also covers: missing-db raises `FileNotFoundError`, pre-existing
  archive raises `FileExistsError`, tampered archive tripped by the
  verify step, pre-existing db moved aside as `.bak-*`, CLI round-trip
  via `monkeypatch.setenv`, and `--no-verify` bypass.

### Changed
- **Safe tar extraction.** `parallax/restore.py` rejects archive entries
  whose resolved paths escape the staging dir, as well as absolute paths
  and non-regular/non-directory members — portable across Python 3.11+
  without relying on `tarfile.data_filter` (3.12+).
- **Version bump** `0.2.0` → `0.2.1` across `pyproject.toml` and
  `parallax/__init__.__version__`.

### Acceptance gate
- `python -m pytest tests/` — **213 passed** (up from v0.2.0's 204).
- Coverage total: **96.15%** (gate remains `--cov-fail-under=80`).
- `ruff check parallax/backup.py parallax/restore.py parallax/cli.py
  tests/test_backup_restore.py` — 0 errors.

## [0.2.0] - 2026-04-18

### Added
- **`tests/acceptance/` — 4-statement SQL acceptance harness (Phase 2 closeout).**
  Four `.sql` files held as the SSoT (`01_canonical.sql`, `02_identity.sql`,
  `03_state_traceable.sql`, `04_rebuild_identical.sql`) plus a single
  `test_acceptance_sql.py` pytest harness that reads each file, parametrizes
  the placeholders against a one-of-everything seed fixture (1 source,
  1 memory, 1 claim, 1 `claim.state_changed` event, 1 `index_state` row),
  and asserts the four Phase 2 acceptance questions at the DB layer:
  - **01 canonical exists** — `COUNT(*)` of `claims` and `memories` is non-zero.
  - **02 identity** — every object has a PK lookup that returns exactly 1 row,
    and the `claims → sources` FK JOIN does not drop the row.
  - **03 state traceable** — any claim's history replays from `events` ordered
    by `created_at`, and `payload_json` parses as JSON.
  - **04 rebuild identical** — `rebuild_index('chroma')` produces a snapshot
    whose `doc_count` and `state` are byte-equivalent across two consecutive
    rebuilds when no source data has changed; only `version` increments.
- **Coverage gate.** `pyproject.toml` now runs pytest with
  `--cov=parallax --cov-report=term-missing --cov-fail-under=80`.
  Future PRs that drop coverage below 80% will fail CI. v0.2.0 ships at
  ~98% total coverage so the gate has substantial headroom.

### Changed
- `pyproject.toml` `[tool.pytest.ini_options].addopts` now wires the
  coverage gate; `[tool.coverage.run]` and `[tool.coverage.report]`
  added to pin the source set to `parallax` and surface missing lines.

### Packaging
- `pyproject.toml` version bumped to 0.2.0; `parallax.__version__` matches.
- This release marks the **Phase 2 closeout**. Phase 2 deliverables not in
  scope for v0.2.0 (`events` full replay, schema introspection dry-run,
  LongMemEval baseline) intentionally defer to v0.3.x.

## [0.1.5] - 2026-04-18

### Fixed
- **Migration framework atomicity (FIX-01).** v0.1.4 used
  `conn.executescript()` in each migration `up()`, which issues an
  implicit `COMMIT` and silently broke the documented "DDL + ledger
  insert succeed-or-fail together" guarantee. v0.1.5 refactors
  `m0001_initial_schema`, `m0002_events_append_only`, and
  `m0003_claim_metadata` to expose `STATEMENTS: list[str]` executed
  individually via `conn.execute(stmt)`, and wraps each migration's
  `up()`/`down()` plus its matching ledger row in an explicit
  `BEGIN IMMEDIATE` ... `COMMIT` block (`_manual_tx` contextmanager,
  `isolation_level = None`). A failure inside `up()` now rolls back
  both the partial DDL and the ledger insert, so partially-applied
  migrations are never recorded as applied.
- **`claim_metadata` FK semantics (FIX-02).** v0.1.4 declared
  `superseded_by TEXT REFERENCES claims(claim_id)` with no `ON DELETE`
  clause (defaulting to `NO ACTION` / `RESTRICT`) and no guard against
  self-supersession. Migration **0005 claim_metadata_fk** recreates
  the table via the SQLite table-swap pattern (CREATE _new + INSERT
  SELECT + DROP + RENAME) to add `ON DELETE SET NULL` on
  `superseded_by` (a deleted successor no longer blocks predecessor
  deletion; the pointer becomes NULL) and
  `CHECK (superseded_by IS NULL OR claim_id != superseded_by)` (a row
  cannot declare itself its own successor). Existing data is preserved
  in-place across the swap; corrupt self-supersession rows would abort
  the migration by design.

### Added
- **Migration 0004 events_user_time_index (FIX-03).** Creates
  `idx_events_user_time ON events(user_id, created_at)` to back the
  watermark scan in `parallax.index._last_event_id` and any per-user
  replay queries. Without this index those queries degrade to a full
  table scan as the events log grows. Verified via `EXPLAIN QUERY PLAN`
  in `tests/test_migrations.py::TestEventsUserTimeIndexFix03`.
- **Migration 0005 claim_metadata_fk (FIX-02).** See the FK fix above.
- `tests/test_migrations.py::TestAtomicityFix01` — proves
  `migrate_to_latest` rolls back both the DDL and the ledger insert
  when a registered migration's `up()` raises mid-way, and proves no
  shipped migration module calls `executescript`.
- `tests/test_migrations.py::TestEventsUserTimeIndexFix03` — proves the
  index is created, that the planner uses it for the watermark query,
  and that `migrate_down_to(3)` drops it.
- `tests/test_migrations.py::TestClaimMetadataV5Fix02` — proves the
  CHECK blocks self-supersession, `ON DELETE SET NULL` clears
  `superseded_by` when the successor is deleted, data is preserved
  across the v5 swap, and a down→up round-trip restores the v5 schema.

### Changed
- `parallax/migrations/__init__.py` docstring rewritten to accurately
  describe the v0.1.5 atomicity guarantee and to record the new
  contract for migration authors: each `up()` MUST issue individual
  `conn.execute(stmt)` calls and MUST NOT call `conn.executescript`
  (which would issue an implicit COMMIT and break atomicity).
- `tests/test_bootstrap.py::test_bootstrap_runs_migrations` and the
  registry-count tests in `tests/test_migrations.py` updated to assert
  five applied versions `[1, 2, 3, 4, 5]`.

### Packaging
- `pyproject.toml` version bumped to 0.1.5; `parallax.__version__` matches.

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
