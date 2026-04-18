# Parallax Kernel — State Transition Matrix

Single source of truth for every stateful entity's allowed state moves. The
vocabulary is pinned by `schema.sql` (see line refs in each section).
Any writer that changes a `state` column MUST consult this table; any
rejected transition should surface as `event_type='*.state_changed'` with
the denial recorded for audit.

Notation: `Y` = allowed, `-` = forbidden. `→` reads "from row to column".
Terminal states (`archived`, `rejected`, `revoked`) show `-` on their own
self-cell by convention — re-entry is meaningless once the sink is reached
and would hide audit intent. See the "Terminal states" footnote per section.
Trigger column names the actor/subsystem that may effect the transition.

---

## memories

Vocabulary (`schema.sql:32`): `draft | active | archived`.

| from → to     | draft | active | archived | Trigger                                          |
|---------------|:-----:|:------:|:--------:|--------------------------------------------------|
| **draft**     |   Y   |   Y    |    Y     | writer (`ingest_memory`) / user edit / archive   |
| **active**    |   -   |   Y    |    Y     | user archive / retention policy                  |
| **archived**  |   -   |   -    |    -     | terminal (see note)                              |

Notes:
- `ingest_memory` inserts with `state='active'` today; `draft` is reserved
  for the extract pipeline (P3) that stages LLM-proposed memories before
  user confirmation.
- Archive is logical, not destructive — the content_hash row is preserved
  so dedup stays correct.
- **Terminal**: `archived` is a sink. Restoring content means inserting a
  NEW row with a fresh content_hash (or re-ingesting the same logical
  content, which dedups). Self-cell is `-` because re-archiving an
  already-archived row is a no-op that should not emit an event.

---

## claims

Vocabulary (`schema.sql:49`): `auto | pending | confirmed | rejected`.

| from → to       | auto | pending | confirmed | rejected | Trigger                                       |
|-----------------|:----:|:-------:|:---------:|:--------:|-----------------------------------------------|
| **auto**        |  Y   |    Y    |     Y     |    Y     | extractor → reviewer queue / user confirm     |
| **pending**     |  -   |    Y    |     Y     |    Y     | user confirm/reject / timeout auto-reject     |
| **confirmed**   |  -   |    -    |     Y     |    Y     | user revoke (moves to rejected)               |
| **rejected**    |  -   |    -    |     -     |    -     | terminal (see note)                           |

Notes:
- `ingest_claim` inserts with `state='auto'`. The transition to `pending`
  happens when a reviewer surface pulls the row; the transition to
  `confirmed`/`rejected` is a user decision recorded via the `decisions`
  table and mirrored onto `claims.state` by the application layer.
- Dedup still applies pre-state-change: `UNIQUE(content_hash, source_id)`
  prevents the same logical claim appearing twice even if one copy is
  confirmed and another is pending.
- **Terminal**: `rejected` is a sink. Re-raising a rejected claim means
  inserting a NEW `auto` row with a fresh `source_id` context.

---

## sources

Vocabulary (`schema.sql:19`): `ingested | parsed | archived`.

| from → to     | ingested | parsed | archived | Trigger                                       |
|---------------|:--------:|:------:|:--------:|-----------------------------------------------|
| **ingested**  |    Y     |   Y    |    Y     | parser job / manual archive                   |
| **parsed**    |    -     |   Y    |    Y     | re-parse is idempotent; archive is terminal   |
| **archived**  |    -     |   -    |    -     | terminal (see note)                           |

Notes:
- Synthetic `direct:<user_id>` sources skip the parser path and stay in
  `ingested` forever. That is intentional: they carry no external payload
  to parse.
- **Terminal**: `archived` is a sink. Re-archiving is a no-op.

---

## decisions

Vocabulary (`schema.sql:66`): `proposed | approved | applied | revoked`.

| from → to     | proposed | approved | applied | revoked | Trigger                                          |
|---------------|:--------:|:--------:|:-------:|:-------:|--------------------------------------------------|
| **proposed**  |    Y     |    Y     |    -    |    Y     | reviewer approve / user cancel                    |
| **approved**  |    -     |    Y     |    Y    |    Y     | executor apply / reviewer revoke                  |
| **applied**   |    -     |    -     |    Y    |    Y     | user revoke (side effects surfaced via events)    |
| **revoked**   |    -     |    -     |    -    |    -     | terminal (see note)                               |

Notes:
- `approval_tier` (schema line 65) gates the allowed approve→apply
  transition; P0 leaves the field empty and treats any `approved` row as
  applyable.
- Revocation from `applied` MUST emit a compensating event chain so the
  downstream projection (claim.state, memory.state) rolls back.
- **Terminal**: `revoked` is a sink. A new decision with fresh
  `decision_id` must be created to re-attempt the action.

---

## events

Events have no `state` column by design — they are append-only. The
append-only contract is currently enforced by `parallax.sqlite_store`
exposing only `insert_event` in its `__all__` list; DB-level BEFORE UPDATE
/ BEFORE DELETE triggers are on the P2 roadmap (see `prd.json`).

---

## Cross-entity invariants

1. **Every referenced row must already exist.** Decision / event writes
   that name a `target_kind` + `target_id` MUST call
   `parallax.validators.target_ref_exists(conn, kind, id)` before insert,
   and MUST do so inside the same transaction as the dependent insert to
   avoid TOCTOU races under WAL-mode SQLite.
2. **decisions.target_kind is narrower than events.target_kind.** The
   hard `CHECK` on `decisions.target_kind` (`schema.sql:61`) allows only
   `claim | memory | source`; decisions never target other decisions in
   the Phase-0 model. `events.target_kind` is intentionally unconstrained
   so the audit log can record decision-level state changes (e.g.
   `event_type='decision.state_changed'` with `target_kind='decision'`).
   Use `parallax.validators.DECISION_TARGET_KINDS` for the narrower
   allow-list and `VALID_TARGET_KINDS` for the wider events-compatible
   set.
3. **Terminal states never re-enter.** `archived`, `rejected`, `revoked`
   are sinks. Restoring content means inserting a NEW row with a fresh
   id (or re-ingesting the same logical content, which dedups).
4. **state transitions are recorded in events.** Every allowed transition
   should emit `event_type='{entity}.state_changed'` with a payload
   recording `before`/`after` states so a rebuild can replay history.
