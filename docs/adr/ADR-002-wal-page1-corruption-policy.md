# ADR-002 — WAL mode + page-1 corruption detection policy

- **Status:** Accepted
- **Date:** 2026-04-18
- **Frozen in:** v0.1.3 (policy); WAL mode itself has been on since v0.1.0

## Context

The fault-injection stress suite exists to guarantee that the SQLite
store never returns silently-corrupted rows: an open following on-disk
damage must either recover a consistent state (WAL replay) or raise
`sqlite3.DatabaseError`. "The read succeeded and handed us garbage" is
the single outcome the store must refuse.

The v0.1.2 implementation of `TestCorruptDB` in
`tests/stress/test_fault_injection.py` appended garbage bytes *after*
the last page of the database file and then re-opened it. The 2-agent
review on v0.1.2 caught that this test is a silent no-op:

> SQLite sizes the database from the page-count field in the header,
> not from `stat()` on the file. Bytes appended past the last page are
> never read, so the "corruption" was invisible and the test would
> have passed even if the store had no corruption detection at all.

The test was therefore not exercising the property it claimed to
guarantee. The fix needed to (a) land the corruption on a byte range
SQLite actually reads and (b) stay deterministic on Windows (no races
with WAL flush, no leftover file handles).

## Decision

The v0.1.3 corruption-detection policy has four parts:

1. **Journal mode is WAL, declared in schema.**
   `PRAGMA journal_mode = WAL` is pinned at the top of `schema.sql`.
   Every `connect(db_path)` opens a WAL-backed DB.

2. **Force a `TRUNCATE` checkpoint before injecting corruption.**
   After writing a committed row, the test runs
   `PRAGMA wal_checkpoint(TRUNCATE)`. This forces the committed pages
   out of the WAL sidecar and into the main DB file, so the bytes we
   are about to corrupt are guaranteed to live on a real on-disk
   page that the next read must touch.

3. **Overwrite 256 bytes inside page 1, starting at offset 100.**
   The SQLite file format reserves bytes 0–99 of page 1 for the
   database header; bytes 100 onward hold the first B-tree page's
   own header and cells. Writing `\xde\xad\xbe\xef * 64` at offset
   100 destroys the B-tree structure *and* at least one cell, both of
   which SQLite must parse to serve any read against the `memories`
   table.

4. **Accept two outcomes; reject the third.**
   After corruption, the test opens the DB and issues
   `SELECT memory_id, content_hash FROM memories`. Accepted outcomes:

   - the open or read raises `sqlite3.DatabaseError` (SQLite detected
     corruption and refused to return rows);
   - the open and read succeed, in which case every returned row's
     `content_hash` must parse as a 64-char hex string and the row
     we inserted pre-corruption must still be present (SQLite
     detected a salvageable state and recovered it).

   Any other outcome — silently truncated result set, garbled rows,
   swallowed error — is a regression.

## Consequences

- The test now exercises SQLite's on-read corruption detection; the
  v0.1.2 trailing-append version touched no bytes SQLite re-reads, so
  it would have passed even with detection disabled.
- The test is deterministic on Windows: the `TRUNCATE` checkpoint
  truncates the WAL sidecar so the committed pages live in the main
  file, and the checkpoint connection is closed before the `r+b`
  overwrite, so no handle-sharing race remains.
- The policy covers single-page damage only. Multi-page corruption,
  corruption inside the WAL sidecar while the DB is open, and
  freelist corruption are explicitly out of scope for v0.1.x — SQLite
  may recover or partially recover from those cases, and the v0.1.3
  test does not attempt to pin which. A future ADR must land before
  the stress suite adds coverage, because the "accept recovery OR
  DatabaseError" rule may have to tighten.
- `PRAGMA journal_mode=WAL` is persistent at the database-file level
  (SQLite records the journal mode in the file header), while
  `PRAGMA foreign_keys=ON` is per-connection and must be re-asserted
  on every open — `parallax.sqlite_store.connect` is the single entry
  point that guarantees both. Hand-rolling `sqlite3.connect(path)`
  skips the FK enforcement and is a latent correctness bug, not a
  corruption bug, but lives in the same PRAGMA blast radius.
- Changing the page-1 overwrite offset or size is a breaking change
  to the policy and requires a follow-up ADR — not a silent test
  tweak.

## Alternatives considered

**Appending garbage after the last page (the v0.1.2 approach)** —
rejected. SQLite reads pages by page-count, not by file size, so
trailing bytes are invisible. The test passes regardless of whether
corruption detection works; it provides zero evidence for the
property we care about.

**Corrupting the 100-byte SQLite header (offset 0..99)** — rejected.
Damaging the magic string or header fields produces a generic
`file is not a database` error at `sqlite3.connect` time. That proves
SQLite rejects a malformed header, which we already trust; it does
*not* prove the B-tree / cell parser surfaces corruption on read,
which is the interesting property. Page-1 B-tree corruption is the
weakest damage that exercises the interesting code path.

**Injecting corruption into the WAL sidecar instead of the main
file** — rejected for now. WAL-sidecar corruption is a legitimate
failure mode but collides with the Windows file-handle model: the
sidecar can be held open by the SQLite VFS between connections,
making deterministic injection flaky on `win32`. Documented as
future work in `tests/stress/REPORT.md`.

**Using `PRAGMA integrity_check` instead of a read-based probe** —
rejected. `integrity_check` is useful, but it is not what application
code runs on the hot path. The test deliberately uses the same query
surface (`SELECT ... FROM memories`) that production callers use, so
"my users' read path detects this" is the property under test, not
"SQLite's maintenance command detects this".

## References

- `tests/stress/test_fault_injection.py::TestCorruptDB::test_page1_corruption_is_not_silent`
  — the regression test implementing the policy.
- `tests/stress/REPORT.md` — stress suite summary, including the
  GIL-concurrency caveat and the out-of-scope list.
- `schema.sql` — `PRAGMA journal_mode = WAL` (top of file) pins WAL
  as the journal mode for all Parallax DBs.
- `CHANGELOG.md`, `[0.1.3] > Fixed` — entry documenting the
  v0.1.2 → v0.1.3 corruption-detection fix.
- SQLite file-format spec, §1.2 "The Database Header" and §1.5
  "The Lock-Byte Page" — header layout that drove the offset-100
  choice.
