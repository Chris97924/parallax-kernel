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

## Observability

Parallax ships with a single-file, stdlib-only telemetry module
(`parallax/telemetry.py`, under 200 lines). No extra dependencies, no
Prometheus exporter -- Prometheus is intentionally out of scope (YAGNI);
call `snapshot()` and adapt to whatever scrape format the caller needs.

### Structured events

All events are emitted as single-line JSON records under the `parallax.*`
logger namespace (keys: `ts`, `level`, `logger`, `msg`, plus any flattened
extras including `event`).

| Event | Level | Emitted when |
|---|---|---|
| `dedup_hit` | INFO | An UPSERT collapses onto an existing `content_hash`. |
| `state_changed` | INFO | A row transitions between lifecycle states. |
| `orphan_rejected` | INFO | A target-less event/decision is rejected at the boundary. |
| `ingest_error` | ERROR | Any exception inside `ingest_memory` / `ingest_claim`. |

### In-memory metrics

Thread-safe counters plus a bounded (1024-sample) latency ring buffer with
nearest-rank percentiles:

- `ingested_total` -- successful `ingest_memory` + `ingest_claim` calls
- `dedup_hits_total` -- content_hash collisions absorbed by UPSERT
- `errors_total` -- exceptions captured by `emit_ingest_error`
- `latency_p50_ms` / `latency_p95_ms` / `latency_p99_ms`
- `last_error` -- timestamped string from the most recent `emit_ingest_error`

### Health snapshot

`parallax.health(db_path)` returns a dict with the DB path (resolved
absolute), per-table row counts, the SQLite `journal_mode` (expected
`"wal"` on a bootstrapped instance), and `last_error`.

```python
from parallax import health, telemetry

# Drive some ingest traffic first (see examples/quickstart.py).
print(telemetry.snapshot())
# {'ingested_total': 42, 'dedup_hits_total': 3, 'errors_total': 0,
#  'latency_p50_ms': 1.8, 'latency_p95_ms': 4.1, 'latency_p99_ms': 7.2,
#  'last_error': None}

print(health("/tmp/my-parallax/db/parallax.db"))
# {'db_path': '/tmp/my-parallax/db/parallax.db',
#  'table_counts': {'sources': 1, 'memories': 12, 'claims': 30,
#                   'decisions': 0, 'events': 0, 'index_state': 0},
#  'journal_mode': 'wal',
#  'last_error': None}
```

## Testing

```bash
pytest
```

GitHub Actions runs the suite on Python 3.11 for every PR and every push
to `main` (`.github/workflows/tests.yml`).

## License

MIT — see [`LICENSE`](./LICENSE).
