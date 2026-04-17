# Parallax-Kernel

Content-addressed canonical knowledge-base store. P0 core: 6-object schema
(`sources`, `memories`, `claims`, `events`, plus supporting indexes) with
`content_hash` dedup on memories and claims, and a race-safe UPSERT ingest
path.

> The GitHub repo is named `parallax-kernel`. The Python package is imported
> as `parallax`, so `pip install parallax-kernel` gives you
> `from parallax import ...`.

## Quick Start

Three reproducible steps from a clean clone:

```bash
# 1. Clone + install (editable + dev extras for tests)
git clone https://github.com/<your-user>/parallax-kernel.git
cd parallax-kernel
pip install -e .[dev]

# 2. Bootstrap a fresh Parallax instance at any path
python bootstrap.py /tmp/my-parallax

# 3. Run the end-to-end quickstart (bootstrap → ingest → retrieve)
python examples/quickstart.py
```

## Public API

Everything re-exported from the `parallax` package root:

| Name | Kind | Role |
|---|---|---|
| `ingest_memory` | function | UPSERT a memory; returns the persisted `memory_id`. |
| `ingest_claim` | function | UPSERT a claim; returns the persisted `claim_id`. |
| `memories_by_user` | function | List memories for a user, optional `state` filter. |
| `claims_by_user` | function | List claims for a user, optional `state` filter. |
| `claims_by_subject` | function | List a user's claims filtered by exact subject. |
| `memory_by_content_hash` | function | Lookup one memory by content_hash (`Optional[dict]`). |
| `claim_by_content_hash` | function | Lookup one claim by content_hash (`Optional[dict]`). |
| `Source` / `Memory` / `Claim` / `Event` | dataclass | Frozen record types used by the storage layer. |
| `__version__` | str | Package version (currently `0.1.1`). |

## Modules

| Module | Role |
|---|---|
| `parallax.hashing` | `normalize(*parts)` + `content_hash(*parts)` — SSoT for dedup keys. |
| `parallax.config` | Frozen `ParallaxConfig` + `load_config()` (env-driven). |
| `parallax.sqlite_store` | Narrow SQLite surface. Events are append-only. |
| `parallax.ingest` | UPSERT `ingest_memory` / `ingest_claim` with synthetic `direct:<user_id>` source. |
| `parallax.retrieve` | Read helpers returning `dict` / `Optional[dict]`. |
| `bootstrap.py` | One-shot initializer + CLI. |

## Testing

```bash
pytest
```

GitHub Actions runs the suite on Python 3.11 for every PR and every push
to `main` (`.github/workflows/tests.yml`).

## License

MIT — see [`LICENSE`](./LICENSE).
