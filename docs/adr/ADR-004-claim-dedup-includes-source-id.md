# ADR-004: Claim dedup content_hash includes source_id

## Status

Accepted — 2026-04-19, v0.4.0.

## Context

`claims.content_hash` is computed from `(subject, predicate, object, source_id)`
by `parallax.hashing.content_hash`, and the UNIQUE index on
`claims(content_hash, source_id)` guarantees that two claims with identical
hash under the same source collapse to a single row.

A recurring P1 MED review question asked whether `source_id` should be part
of the hash at all, or whether dedup should happen on `(subject, predicate,
object)` alone with `source_id` treated as provenance metadata attached
downstream. That direction would make `Chris --likes--> coffee` emitted by
the Discord extractor identical to the same triple ingested from the
Obsidian vault, which is an intuitive "cross-source corroboration" view of
the knowledge base.

The existing code, schema, and live migrated DB (2123 claims from the a2a
migration) all depend on the opposite direction: same triple under different
sources is two distinct claims. v0.4.0 locks this choice into an ADR +
regression tests so the question stops coming back every review cycle.

## Decision

`claims.content_hash = sha256(normalize(subject || predicate || object || source_id))`.

`source_id` is PART of the claim dedup key. Two claims with identical
`(subject, predicate, object)` but different `source_id` produce two
distinct `content_hash` values and therefore two distinct rows in
`claims`.

The schema comment at `E:/Parallax/schema.sql:47` states this formula
directly; `parallax/ingest.py:ingest_claim` computes the hash via
`content_hash(subject, predicate, object_, source_id)`; the UNIQUE index
`uniq_claims_content(content_hash, source_id)` matches.

Synthetic direct sources (`direct:<user_id>`) close the UNIQUE NULL-hole:
`claims.source_id` is NOT NULL in the schema, so every claim has a
defined source_id and therefore a defined position in the dedup
keyspace.

## Consequences

- Cross-source corroboration of the same triple is an **explicit
  operation**, not a silent row collapse. A future "promote claim"
  pipeline will need to scan the claims table for matching
  `(subject, predicate, object)` across `source_id` values and emit a
  `claim.corroborated` event — that feature is out of scope for v0.4.0
  and tracked as Phase-4 debt.
- Per-source provenance is preserved by construction. Retrieval can
  answer "which sources asserted X?" with a single
  `SELECT source_id FROM claims WHERE subject = ? AND predicate = ? AND
  object = ?`.
- A claim originating from the synthetic `direct:<user_id>` source and
  a claim originating from a real source_id produce different hashes
  even when the triple is identical — this is intentional and
  preserves the audit trail of "the user told us this directly vs. we
  extracted it from a document."
- Backfill / migration scripts that want to normalize identical triples
  across sources MUST do it at the application layer, not by mutating
  `content_hash`.

## Alternatives considered

1. **Dedup on `(subject, predicate, object)` only; treat `source_id`
   as metadata.** Rejected because:
   - Provenance signal would be lost on write — which source "owned"
     the first copy is no longer recoverable without a side table.
   - The claim-promotion pipeline (Phase-4) depends on per-source
     distinctness to reason about corroboration weight.
   - The a2a migration produced 2123 claims under the current
     semantics; re-hashing them in-place would collide ~30% onto
     existing rows and silently lose the source dimension.

2. **Drop `source_id` from the hash but keep `(content_hash, source_id)`
   UNIQUE in the index.** Rejected because:
   - The hash no longer matches the UNIQUE constraint, which confuses
     the dedup semantics for reviewers reading `ingest_claim`.
   - Two rows with the same content_hash but different source_ids look
     like a dedup bug to anyone scanning the table without the ADR.

3. **Emit `claim.corroborated` automatically on identical triple +
   new source.** Rejected for v0.4.0 because the promotion logic
   needs the `decisions` layer (not yet wired) to decide whether
   corroboration graduates a claim to `state='confirmed'`. Deferred to
   Phase-4.

## References

- `E:/Parallax/parallax/hashing.py` — canonical hash formula.
- `E:/Parallax/parallax/ingest.py:ingest_claim` — hash call site and
  race-safe UPSERT on `(content_hash, source_id)`.
- `E:/Parallax/schema.sql:40-55` — claims table + UNIQUE index
  declaration.
- `E:/Parallax/migration/a2a_to_parallax.py` — migration that produced
  the live 2123-claim corpus under these semantics.
- `E:/Parallax/tests/test_claim_dedup_semantics.py` — regression that
  locks this decision.
- [ADR-001](ADR-001-content-hash-ssot.md) — parent decision on
  `content_hash` construction.
