# Architecture Decision Records

This directory captures load-bearing design decisions for the Parallax kernel. An ADR is a short document that names one decision, the context that forced it, the alternatives that were rejected, and the consequences the project now lives with.

ADRs are written when a decision is (a) non-obvious to a future reader, (b) expensive to reverse, or (c) already frozen into shipped schema, tests, or public API. See the repository `CONTRIBUTING.md` for the workflow and when to write a new ADR.

## Status lifecycle

| Status | Meaning |
|---|---|
| `Proposed` | Drafted but not yet accepted. May still change shape. |
| `Accepted` | The decision is in force. Code, schema, and tests reflect it. |
| `Deprecated` | No longer in force, but not replaced by anything specific. Historical. |
| `Superseded-by-ADR-NNN` | Replaced by a later ADR. The replacement link is mandatory. |

Once an ADR reaches `Accepted`, the file is treated as append-only. Changing the decision means writing a new ADR and marking the old one `Superseded-by-ADR-NNN`.

## Index

| ADR | Status | Summary |
|---|---|---|
| [ADR-001](ADR-001-content-hash-ssot.md) | Accepted | `content_hash = sha256(NFC-strip + "||" join)` — one function, one module. UUIDv4 (random) and UUIDv5 (namespaced) both rejected because they lack cross-machine idempotence or direct reproducibility from the schema comment. |
| [ADR-002](ADR-002-wal-page1-corruption-policy.md) | Accepted | WAL mode is on; corruption detection injects 256 bytes inside page 1 (offset 100) after a TRUNCATE checkpoint. Trailing-append (the v0.1.2 approach) is silent to SQLite and was rejected. |
| [ADR-003](ADR-003-target-kind-split.md) | Accepted | `events.target_kind` is intentionally unconstrained so the audit log can record decision-level state changes; `decisions.target_kind` is hard-CHECK'd to `{memory, claim, source}`. A symmetric CHECK on both would make legitimate audit rows illegal. |
| [ADR-004](ADR-004-claim-dedup-includes-source-id.md) | Accepted | `claims.content_hash` includes `source_id`; identical triple under different sources is two rows by design. Cross-source corroboration is an explicit future operation, not a silent collapse. |
| [ADR-005](ADR-005-claim-content-hash-user-id-scope.md) | Accepted | `claims.content_hash` also scopes to `user_id` (5-part formula, supersedes ADR-004's 4-part formula). Two users sharing one source each own their own claim rows; the UNIQUE index becomes `(content_hash, source_id, user_id)`. |
| [ADR-006](ADR-006-retrieval-filtered-pipeline.md) | Proposed | Retrieval-filtered answerer pipeline: six-intent closed set with deterministic priority, two-layer router (rule ≥ 0.80 / Flash ≥ 0.70), MMR fallback floor (`fallback_e2e ≥ 0.95 × baseline` CI gate), evidence-only answerer with `insufficient_evidence` abstain token, and a frozen six-number A/B evaluation tuple. |

## Numbering

ADRs are numbered monotonically and never reused. Filename convention: `ADR-NNN-<kebab-slug>.md` where `NNN` is zero-padded to three digits.
