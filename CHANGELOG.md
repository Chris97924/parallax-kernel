# Changelog

All notable changes to this project are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
