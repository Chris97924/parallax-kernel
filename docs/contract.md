# Parallax Kernel — Public API Contract

This document is the canonical mapping of which public APIs **mutate
storage**, which **only write to the event log**, and which are pure
**reads**. The split exists because some events historically were named
in a way that suggested mutation (e.g. `record_claim_state_changed`)
while only emitting an audit row, and downstream agents reading the
README would have followed that mistake into production.

If you ever modify these APIs, update this file in the same PR. Tests
in `tests/test_state_transitions.py` pin the README "State Machine"
example against the implementation so contract drift is caught at CI.

## Conventions

* **Mutates storage** = writes to `sources` / `memories` / `claims` /
  `decisions` / `index_state`.
* **Logs only** = appends to `events` (append-only by DB trigger from
  m0008 onward) without touching the kind-tables.
* **Reads** = no writes of any kind; safe in read-only replicas.
* All lookups that take a `content_hash` argument are user-scoped: the
  caller MUST provide `user_id` as a keyword-only argument.

## API table

| Function | Module | Mutates storage | Logs an event | Notes |
|---|---|---|---|---|
| `ingest_memory` | `parallax.ingest` | ✅ `memories` | ✅ `memory.created` (first ingest) / `memory.reaffirmed` (dedup hit) | Idempotent on `(content_hash, user_id)`. |
| `ingest_claim` | `parallax.ingest` | ✅ `claims` | ✅ `claim.created` (first ingest) / `claim.reaffirmed` (dedup hit) | Idempotent on `(content_hash, source_id, user_id)`. |
| `transition_claim_state` | `parallax.events` | ✅ `claims.state` + `claims.updated_at` | ✅ `claim.state_changed` | Atomic. Validates via `is_allowed_transition`. **Use this for state changes.** |
| `record_claim_state_changed` | `parallax.events` | ❌ | ✅ `claim.state_changed` | **Only writes the audit event.** Pair with the matching `UPDATE claims` in the same transaction or use `transition_claim_state`. |
| `record_memory_reaffirmed` | `parallax.events` | ❌ | ✅ `memory.reaffirmed` | Audit only. |
| `record_event` | `parallax.events` | ❌ | ✅ `<arbitrary>` | Lower-level append-only writer. Caller picks event_type. |
| `reaffirm` | `parallax.sqlite_store` | ❌ | ✅ `<kind>.reaffirmed` | Audit only. |
| `rebuild_index` | `parallax.index` | ✅ `index_state` (appends a new history row) | ❌ | Deterministic per DB snapshot, not DB-idempotent — see [README → Modules](../README.md#modules). |
| `replay_events` | `parallax.replay` | ✅ `memories` / `claims` (rebuild) | ❌ | Reads `events`, applies create / state_changed events into a target connection. **Deliberately bypasses `is_allowed_transition`** so historical transitions remain replayable across rule tightening — this is the one documented exception to the "atomic transition" contract above. |
| `ingest_hook` | `parallax.hooks` | ✅ `memories` / `claims` (via ingest helpers) | ✅ `<kind>.created` / `<kind>.reaffirmed` | Thin wrapper over `ingest_memory` / `ingest_claim` for hook-based callers. Inherits all idempotency + audit behaviour of the underlying helpers. |
| `ingest_from_json` | `parallax.hooks` | ✅ `memories` / `claims` | ✅ `<kind>.created` / `<kind>.reaffirmed` | JSON-payload variant of `ingest_hook`. |
| `build_session_reminder` | `parallax.injector` | ❌ | ❌ | Read-only — composes a `<system-reminder>` block from existing rows. |
| `backfill_creation_events` | `parallax.replay` | ❌ | ✅ `<kind>.created` | Synthesizes create events for pre-0.4.0 rows. Idempotent. |
| `migrate_to_latest` | `parallax.migrations` | ✅ schema + ledger | ❌ | Each migration runs in its own explicit transaction. |
| `migrate_down_to` | `parallax.migrations` | ✅ schema + ledger | ❌ | Symmetric `down()` for each migration. |
| `memory_by_content_hash` | `parallax.retrieve` | ❌ | ❌ | User-scoped lookup. `user_id` keyword-only. |
| `claim_by_content_hash` | `parallax.retrieve` | ❌ | ❌ | User-scoped lookup. `user_id` keyword-only. |
| `memories_by_user` / `claims_by_user` / `claims_by_subject` | `parallax.retrieve` | ❌ | ❌ | User-scoped reads. |
| `recent_context` / `by_file` / `by_decision` / `by_bug_fix` / `by_timeline` / `by_entity` | `parallax.retrieve` | ❌ | ❌ | v0.3.0 explicit retrieval API. |
| `explain_retrieve` | `parallax.retrieve` | ❌ | ❌ | Returns a `RetrievalTrace`; debug-only. |
| `is_allowed_transition` | `parallax.transitions` | ❌ | ❌ | Pure check against `(MEMORY|CLAIM|SOURCE|DECISION)_TRANSITIONS`. |
| `target_ref_exists` | `parallax.validators` | ❌ | ❌ | Boundary check used by `record_event` to reject orphan targets. |
| `parallax_info` / `health` | `parallax.introspection` / `parallax.telemetry` | ❌ | ❌ | Read-only runtime introspection. |

## State transition rules

`(MEMORY|CLAIM|SOURCE|DECISION)_TRANSITIONS` is the SSoT for allowed
state moves. Every value is a `dict[str, frozenset[str]]` mapping a
state to its allowed next states (including self-loops on non-terminal
states). Use `is_allowed_transition(entity, from_state, to_state)` for
the boolean check; consult the dict directly only when you need to
inspect or render the table.

## Choosing the right state-change API

```
Did you already UPDATE the row in the same transaction?
├── Yes  → call record_claim_state_changed(...) to append the audit event.
└── No   → call transition_claim_state(...) — atomic SELECT + check + UPDATE + event.
```

`parallax.extract.review._transition` is the reference implementation of
the "yes I'm doing the UPDATE myself" path: it pairs an explicit
`UPDATE ... WHERE state='pending'` (with rowcount-based TOCTOU guard)
with `record_claim_state_changed`. New callers without that stricter
"from-pending-only" requirement should default to
`transition_claim_state`.

## Event log invariants

* `events` is append-only. Migration `m0008` installs an `INSTEAD OF
  DELETE` / `INSTEAD OF UPDATE` trigger pair so even direct SQL cannot
  mutate the log.
* Every event carries `(event_id, user_id, actor, event_type,
  target_kind, target_id, payload_json, approval_tier, created_at,
  session_id)`.
* `record_event` rejects orphan targets when `target_kind` is in
  `VALID_TARGET_KINDS`: the row referenced by `(target_kind, target_id)`
  must exist on the same connection.

## See also

* [`README.md`](../README.md) — public API table + state machine example.
* [`ARCHITECTURE.md`](../ARCHITECTURE.md) — system design overview.
* [`docs/state-transitions.md`](state-transitions.md) — the human-readable
  transition matrix mirrored by `parallax.transitions`.
