# Parallax dual-write shadow

Phase 3 introduces `parallax.extract` as an optional layer that extracts
claims from free-form text via a pluggable `Provider`. Rather than
switching the a2a vault writer over immediately, Phase 3 runs
Parallax-side writes **in shadow**: the vault-primary path is unchanged,
and every shadow ingest emits a structured log record so divergence can
be measured offline.

## Feature flag

```python
import os
from parallax.extract.shadow import shadow_write

if os.environ.get("PARALLAX_DUAL_WRITE") == "1":
    claim_ids = shadow_write(
        conn,
        text,
        provider=provider,
        user_id=user_id,
        source_id=source_id,
    )
    # shadow never raises; claim_ids is possibly []
# primary path — unchanged
write_to_vault(...)
```

`shadow_write` wraps `extract_and_ingest` with a blanket
`try/except`. Any provider failure, ingest error, or database exception
is swallowed and logged; the primary vault write is never blocked by the
shadow.

## Log record format

Every call emits exactly one `logger.info` record on
`parallax_shadow_write` with this `extra`:

| field        | type         | meaning                                     |
|--------------|--------------|---------------------------------------------|
| `user_id`    | str          | user the ingest was scoped to               |
| `source_id`  | str \| None  | explicit source, or `None` for synthetic    |
| `count`      | int          | number of claims persisted (0 on failure)   |
| `elapsed_ms` | float        | wall-clock time for the shadow call         |
| `error`      | str (opt.)   | `repr(exc)` — present only on failure path  |

Ship these records to the same log drain that monitors vault writes.
Compare `count` against the vault write's claim count to build a
divergence timeseries — that data decides when the shadow becomes
primary.

## Running the nightly integration check

```bash
pip install 'parallax-kernel[extract]'
OPENROUTER_API_KEY=sk-... python -m pytest tests/integration/ \
    -m llm_integration -q
```

Without the key, the test skips cleanly (no failure). Without the
`[extract]` extra installed, `parallax.extract.providers.openrouter`
imports fail at import time — this is by design so the core install
never pulls `httpx`.
