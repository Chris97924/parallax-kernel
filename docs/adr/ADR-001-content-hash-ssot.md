# ADR-001 — `content_hash` SSoT: `sha256(NFC-strip + "||" join)`

- **Status:** Accepted
- **Date:** 2026-04-18
- **Frozen in:** v0.1.0 (implementation); None-handling tightened to `TypeError` in v0.1.2; confirmed in v0.1.3

## Context

Parallax deduplicates ingested rows by content — re-ingesting the same
memory or claim must converge on the same row, not create a second one.
Two properties drive the design:

1. **Idempotent re-ingest across time and machines.** The same inputs
   on a different laptop, on a different day, must compute the same
   row identity with no coordination.
2. **Mixed-language content.** CJK and Latin text coexist in the
   ingest pipeline (Discord, file uploads, direct input). Unicode
   normalization forms differ across OSes and IMEs, so visually
   identical strings can carry different byte sequences.

Without a canonical content identity, either (a) the same paragraph
re-ingested tomorrow produces a duplicate row, or (b) callers hand-roll
their own dedup, each subtly different.

The v0.1.2 2-agent review also surfaced a subtler failure mode: if
`normalize(None)` silently collapses to `""`, a memory with
`title=None, summary="body"` and a memory with `title="", summary="body"`
hash to the same digest. That is a silent Optional-boundary bug waiting
to happen, and it was fixable only at the hashing boundary.

## Decision

`content_hash` is produced by one function, in one module, with one
algorithm:

```
content_hash(*parts) = sha256(
    "||".join(
        unicodedata.normalize("NFC", part).strip()
        for part in parts
    )
).hexdigest()
```

- Implementation: `parallax/hashing.py::normalize` and
  `parallax/hashing.py::content_hash`.
- Separator: the two-character literal `||` (`_SEPARATOR` in
  `hashing.py`).
- Encoding: `utf-8` before the sha256.
- Output: 64-char lowercase hex digest.

The schema declares, per table, which columns participate in the hash.
Any writer that computes a `content_hash` MUST funnel through
`parallax.hashing.content_hash` — new call sites never hand-roll the
normalization. Current call sites:

- `memories.content_hash = sha256(normalize(title || summary || vault_path))`
  (`schema.sql`, `memories` table).
- `claims.content_hash   = sha256(normalize(subject || predicate || object || source_id))`
  (`schema.sql`, `claims` table).
- `sources.content_hash  = content_hash(source_id)` — degenerate
  single-part call inside `parallax/ingest.py::_ensure_direct_source`;
  listed here so the "funnel through `parallax.hashing`" rule stays
  total and reviewers do not read the two-table case as exhaustive.

`normalize` raises `TypeError` when any part is `None`. Callers holding
`Optional[str]` values MUST convert to `""` at their own boundary (see
`parallax/ingest.py::ingest_memory` for the reference pattern).

## Consequences

- `UNIQUE(content_hash, user_id)` on `memories` and
  `UNIQUE(content_hash, source_id)` on `claims` are load-bearing
  indexes. Dropping either re-opens the dedup hole.
- Re-ingest of the exact same content converges to the same row on
  any machine, on any day, without coordination. This is the property
  that makes `INSERT OR IGNORE` + `SELECT`-by-unique-key a correct
  UPSERT (see `parallax/ingest.py`).
- The two canonical part lists above are part of the public contract.
  Changing the order of columns in the hash is a breaking migration
  and would orphan every existing row.
- **Separator-collision caveat.** A literal `||` inside one of the
  parts shifts the split boundary and still hashes: `("a||b", "c")`
  and `("a", "b||c")` produce the same digest. The schema chooses
  part lists whose members cannot meaningfully contain the separator
  (`vault_path`, `predicate`, etc.) — callers must not invent new
  part lists that carry user-supplied text with `||`. Callers that
  need to tunnel arbitrary user-supplied text through the hash MUST
  escape `||` or add a length-prefix; this is not done globally
  because no current call site needs it.
- `None` is an error, not a hashable value. `Optional[str]` handling
  lives at the ingest boundary, not inside `normalize`.
- **Trailing empty parts shift the hash.** Because `"||".join(...)`
  emits a separator between *every* adjacent pair, calling
  `content_hash("a", "b")` and `content_hash("a", "b", "")` produce
  different digests (`"a||b"` vs `"a||b||"`). Callers that accept
  variable-arity inputs must commit to a fixed part-list per table,
  not drop trailing empties as an "optimization".

## Alternatives considered

**UUID or random id per row** — rejected. UUIDs are unique by
construction, but they carry no information about the content, so:

- re-ingesting the same memory generates a fresh UUID and a duplicate
  row (no idempotence);
- two processes ingesting the same content cannot agree on a row
  identity without round-tripping through a central coordinator;
- dedup becomes an expensive content-equality query rather than a
  primary-key lookup.

A content-addressed hash gets idempotence and cross-process agreement
for free; random ids do not.

**UUIDv5 with a fixed namespace** — rejected. UUIDv5 also hashes the
namespace + name, so it gives the same cross-version, cross-machine
"same content → same id" property as our sha256, but it wraps the
digest in an opaque 128-bit UUID layer (version/variant bits) that
callers cannot verify by re-hashing the canonical string. A documented
`sha256(NFC-strip + "||" join)` is directly reproducible from the
schema comment; a UUIDv5 is not.

**NFKC normalization instead of NFC** — rejected. NFKC collapses
compatibility characters (e.g., full-width digits → half-width), which
changes the meaning of ingested text rather than normalizing its
encoding. NFC is the tightest form that leaves semantics alone while
eliminating encoding-level duplicates.

**`None` → `""` coercion inside `normalize`** — rejected in v0.1.2.
Silently coercing `None` to the empty string makes a memory with
`title=None` hash-equal to a memory with `title=""`, which is a latent
bug the moment any caller uses `Optional[str]` in a dataclass. Raising
`TypeError` forces the boundary to be explicit; the single ingest
site that needs empty-string semantics does the conversion in one
documented place.

**A structured digest (e.g., JSON-canonical form)** — rejected for
now. Canonical JSON would close the separator-collision caveat, but
it introduces a larger spec surface (key ordering, whitespace, number
representation) and no current caller needs it. If a future caller
needs to hash user-supplied free text that can legitimately contain
`||`, revisit this alternative rather than patching around it.

## References

- `parallax/hashing.py` — the single implementation (`normalize`,
  `content_hash`).
- `schema.sql`, `memories` table — comment declares
  `content_hash = sha256(normalize(title||summary||vault_path))`.
- `schema.sql`, `claims` table — comment declares
  `content_hash = sha256(normalize(subject||predicate||object||source_id))`.
- `schema.sql` — `uniq_memories_content`, `uniq_claims_content`
  UNIQUE indexes that enforce dedup.
- `parallax/ingest.py` — reference pattern for `Optional[str]` → `""`
  conversion before calling `content_hash`.
- `CHANGELOG.md`, `[0.1.2]` — `normalize` breaking change from
  silent-None-coerce to `TypeError`.
- `tests/test_hashing.py` — regression tests pinning the contract
  (NFC collapse, strip semantics, `None` → `TypeError`, separator
  shape, hex digest length).
