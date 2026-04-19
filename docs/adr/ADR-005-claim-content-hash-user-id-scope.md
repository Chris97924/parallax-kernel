# ADR-005: Claim content_hash scopes to user_id

## Status

Accepted — 2026-04-19, v0.5.0-pre1.

Supersedes the 4-part formula from [ADR-004](ADR-004-claim-dedup-includes-source-id.md)
by extending the hash input to 5 parts. The dedup-per-source semantic from
ADR-004 is preserved and made strictly per-user.

## Context

Under ADR-004 (v0.4.0) the claim content_hash was
`sha256(normalize(subject || predicate || object || source_id))` and the
UNIQUE index was `(content_hash, source_id)`. That formula assumed every
source_id was owned by exactly one user, which is true today for the
synthetic `direct:<user_id>` source and for the current a2a migration
output.

The v0.5.0 LongMemEval baseline changes the assumption: LongMemEval is a
single-user benchmark, but the retrieval stack is also going to be
exercised against multi-user fixtures where the same source document
(e.g. a shared team wiki URL, a shared ingest queue from Discord) can
feed claims into multiple users' stores. Two users ingesting
``(chris, likes, coffee)`` from the shared source currently produce the
SAME `content_hash`, and `INSERT OR IGNORE` on
`UNIQUE(content_hash, source_id)` silently keeps only the first writer's
claim. The second user's knowledge vanishes.

A three-way review on 2026-04-19 (opus + sonnet + Claude filesystem
verification) classified this as a 🟡 bug: safe for LongMemEval itself
(single-user fixture) but a trip-wire for every other retrieval path
that will share sources across users. Rather than sidestep it with an
application-layer scope-check, we close the hole in the hash itself.

## Decision

`claims.content_hash = sha256(normalize(subject || predicate || object || source_id || user_id))`.

The UNIQUE index becomes `claims(content_hash, source_id, user_id)`.
Both are set by `parallax/migrations/m0007_claim_content_hash_user_id.py`,
which:

1. Drops the old `uniq_claims_content(content_hash, source_id)` index so
   the intermediate rehash sweep doesn't collide on stale UNIQUE
   constraint state.
2. Rehashes every existing claim row in Python using the new 5-part
   formula.
3. Recreates `uniq_claims_content` on the 3-column key.

`parallax/ingest.py:ingest_claim` computes the hash via
`content_hash(subject, predicate, object_, source_id, user_id)` so new
writes and the migrated corpus converge on the same formula.

`schema.sql` is updated so a fresh DB bootstrapped without the migration
framework lands on the same index + comment.

## Consequences

- Two users sharing one source_id now produce two distinct claim rows
  for the same triple — this is the intended behaviour; per-user
  knowledge is never silently merged.
- Downstream code that expected `SELECT content_hash FROM claims WHERE
  subject=? AND predicate=? AND object=? AND source_id=?` to collapse
  duplicates across users must now also scope by `user_id`. No current
  call site does this (ingest_claim's re-select already scopes by
  `content_hash + source_id` and the new hash is user-scoped by
  construction).
- The synthetic `direct:<user_id>` source is unchanged — it was already
  per-user, so adding user_id to its claim hashes doesn't alter dedup
  behaviour for that code path.
- All existing 2123 a2a-migrated claims will be rehashed by m0007 on the
  next `migrate_to_latest`. Since a2a rows are single-user, no new dedup
  collisions are possible; the migration is idempotent.
- `migrate_down_to(6)` reverses the rehash to the 4-part formula and
  restores the 2-column UNIQUE index, so the change is reversible.

## Alternatives considered

1. **Enforce user-scope at the application layer only** (e.g. add
   `AND user_id = ?` to every claim-dedup SELECT and trust the UNIQUE
   index to be a superset constraint). Rejected because:
   - The UNIQUE index would still reject the second writer's INSERT at
     the DB layer, so the application-layer guard can't save the write.
   - Adds silent invariants that leak across modules and drift over
     time.

2. **Add a user_id column to the UNIQUE index without changing the
   hash.** Rejected because:
   - `content_hash` would then no longer be the dedup key — the index
     would have to composite `content_hash + source_id + user_id` but
     the hash input stays 4-part. Two writers for the same
     (triple, source) would still produce identical content_hash, and
     the composite index would accept both — at the cost of
     `content_hash` no longer being a sufficient dedup key on its own.
     Cognitively confusing and schema-inconsistent.

3. **Per-user source_id synthesis for every source (re-id shared
   sources at ingest time).** Rejected because:
   - Destroys the "this claim came from document X" provenance signal;
     two users citing the same wiki would have different source_ids and
     cross-user corroboration queries would require a side table.

## References

- `E:/Parallax/parallax/hashing.py` — canonical hash formula (variadic).
- `E:/Parallax/parallax/ingest.py:ingest_claim` — 5-part call site.
- `E:/Parallax/schema.sql` — updated `claims` comment + UNIQUE index.
- `E:/Parallax/parallax/migrations/m0007_claim_content_hash_user_id.py`
  — rehash + index swap.
- `E:/Parallax/tests/test_content_hash_user_id_scope.py` — cross-user
  regression + migration rehash verification.
- [ADR-001](ADR-001-content-hash-ssot.md) — parent decision on
  `content_hash` construction.
- [ADR-004](ADR-004-claim-dedup-includes-source-id.md) — 4-part formula
  this supersedes.
