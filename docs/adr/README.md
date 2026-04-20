# Architecture Decision Records

This directory captures load-bearing design decisions for the Parallax
kernel as Architecture Decision Records (ADRs). An ADR is a short
document that names one decision, the context that forced it, the
alternatives that were rejected, and the consequences the project now
lives with.

ADRs are written when a decision is (a) non-obvious to a future reader,
(b) expensive to reverse, or (c) already frozen into shipped schema,
tests, or public API. They are *not* a substitute for the code — the
code is still the source of truth for behavior. ADRs exist so that the
*reasoning* behind the code survives the reviewer and the calendar.

## Status lifecycle

Every ADR carries one of four statuses:

| Status                   | Meaning                                                                |
| ------------------------ | ---------------------------------------------------------------------- |
| `Proposed`               | Drafted but not yet accepted. May still change shape.                   |
| `Accepted`               | The decision is in force. Code, schema, and tests reflect it.           |
| `Deprecated`             | No longer in force, but not replaced by anything specific. Historical.  |
| `Superseded-by-ADR-NNN`  | Replaced by a later ADR. The replacement link is mandatory.             |

Once an ADR reaches `Accepted`, the file is treated as append-only.
Changing the decision means writing a new ADR and marking the old one
`Superseded-by-ADR-NNN` — never edit a past decision in place.

**Accepted → Superseded trigger.** An Accepted ADR moves to
`Superseded-by-ADR-NNN` when (a) a code review lands that contradicts
the decision and the reviewer opens a replacement ADR, or (b) a schema
migration in `CHANGELOG.md` changes the contract the ADR pinned. The
replacement ADR's author is responsible for editing the *old* ADR's
Status line to `Superseded-by-ADR-NNN` in the same PR — leaving the
old ADR at `Accepted` while a newer ADR contradicts it is a review
blocker. Typo fixes and reference-link repairs are not supersessions
and may edit in place.

## Numbering and filenames

ADRs are numbered monotonically and never reused. The filename
convention is:

```
ADR-NNN-<kebab-slug>.md
```

- `NNN` is zero-padded to three digits (`001`, `002`, ... `042`).
- `<kebab-slug>` is a short lowercase-hyphen description, matching the
  title closely enough to grep for.
- Deprecating an ADR does *not* free its number. `ADR-007` stays
  `ADR-007` forever, even if superseded by `ADR-042`.

## Template

Every ADR uses the same six sections in this order:

1. **Status** — one of the four statuses above, plus a date in
   `YYYY-MM-DD` and the release/version that froze the decision if
   known (e.g., `v0.1.3`).
2. **Context** — what forced the decision. Include the concrete
   trigger: a bug, a review finding, a schema constraint, a perf
   number. Past-tense, no marketing.
3. **Decision** — the decision itself, stated plainly. Name the
   algorithm, the constant, the schema line, or the contract that
   implements it. A future reader should be able to grep from the ADR
   straight to the code.
4. **Consequences** — what now becomes true because of the decision:
   downstream constraints, known limitations, follow-on work.
5. **Alternatives considered** — options that were weighed and
   rejected, each with the reason. "We didn't think of it" is not an
   alternative; only things that were actually considered.
6. **References** — file paths with line hints, commit/PR links, the
   CHANGELOG entry, related ADRs.

## Index

| ADR                                              | Status   | Summary                                                                       |
| ------------------------------------------------ | -------- | ----------------------------------------------------------------------------- |
| [ADR-001](ADR-001-content-hash-ssot.md)           | Accepted | `content_hash` = `sha256(NFC-strip + '||' join)`; one implementation — UUIDv4 (random) and UUIDv5 (namespaced) both rejected. |
| [ADR-002](ADR-002-wal-page1-corruption-policy.md) | Accepted | WAL mode + inject corruption inside page 1 after a TRUNCATE checkpoint.        |
| [ADR-003](ADR-003-target-kind-split.md)           | Accepted | `events.target_kind` intentionally unconstrained; `decisions.target_kind` hard-CHECKed to `{memory,claim,source}`. |
| [ADR-004](ADR-004-claim-dedup-includes-source-id.md) | Accepted | `claims.content_hash` includes `source_id`; identical triple under different sources is two rows by design. |
| [ADR-005](ADR-005-claim-content-hash-user-id-scope.md) | Accepted | `claims.content_hash` also scopes to `user_id`; supersedes ADR-004's 4-part formula with a 5-part formula + 3-column UNIQUE index. |
| [ADR-006](ADR-006-retrieval-filtered-pipeline.md) | Proposed | xcouncil Phase 1 retrieval-filtered pipeline: six-intent closed set (priority INITIAL) + two-layer router (initial thresholds `>= 0.80` / `>= 0.70`, calibration-on-Accepted) + MMR fallback as empirically-monitored floor (`fallback_e2e >= 0.95 × baseline` CI gate) + evidence-only answerer with `insufficient_evidence` abstain token + six-number A/B tuple including `fallback_e2e`. |

## When to write a new ADR

Write an ADR when any of the following is true:

- A code review forces a non-obvious tradeoff that a future reader will
  re-debate without help (e.g., "why sha256 and not UUID").
- A schema constraint is frozen into `schema.sql` and removing it
  would be a migration.
- A test asserts an invariant whose *reason* is not obvious from the
  test body.
- A public API boundary chooses one shape out of several reasonable
  ones (e.g., rejecting `None` at the hashing boundary).

Do *not* write an ADR for:

- Style choices that are covered by the linter / formatter.
- Decisions that are fully self-explanatory from the code.
- Speculative future work with no caller — ADRs document *frozen*
  decisions, not roadmap items.
