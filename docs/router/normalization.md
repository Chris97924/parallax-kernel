# Router Field Normalization (Lane D-3 / US-D3-04)

`parallax.router.normalize` is the single source of truth for alias precedence
across the MEMORY_ROUTER ingest path and the read-side DTO `body` projection.
This document captures the alias tuples, their precedence, and the explicit
"flag-off" behavior consumers must handle.

## Why a single source of truth

Sonnet Critic's xcouncil Round 2 review flagged that the same logical fact
appears under different keys depending on which legacy code path produced
the row:

| Logical fact | Legacy storage |
|--------------|----------------|
| "the body of a memory" | `memory.summary` (long-form) and event `payload_text` |
| "the object of a claim" | `claim.object_` (Python attr) and `claim.object` (column) |
| "the subject of a claim" | `claim.subject`, but old code uses `entity` |
| "the predicate of a claim" | `claim.predicate`, but events use `event_type` |

If ingest-side normalization and read-side DTO projection use different
precedence, identical input can produce divergent output. We avoid this by
sharing the helper `_first_non_empty(payload, keys, *, field)`.

## Alias tuples

All tuples live in `parallax/router/real_adapter.py`. Declared order is the
canonical precedence — first key in the tuple whose value is a non-empty
`str` wins.

### Memory body (`MEMORY_BODY_KEYS`)

```
("body", "object_", "object", "payload_text", "text", "summary", "description")
```

Persisted as `memories.summary`. The `body` alias is the preferred shape
going forward; the others exist for compatibility with legacy producers
(events, MCP tools, batch importers).

### Memory title (`MEMORY_TITLE_KEYS`)

```
("title", "name")
```

Persisted as `memories.title`. Optional — if no alias resolves, `title` is
stored as `NULL` (not coerced to empty string).

### Claim object (`CLAIM_OBJECT_KEYS`)

```
("object_", "object", "body", "payload_text", "text", "summary")
```

`object_` is the canonical Python attribute (trailing underscore avoids the
built-in keyword); the SQLite column is `object`. The router accepts both.

### Claim subject (`CLAIM_SUBJECT_KEYS`)

```
("subject", "entity", "name")
```

### Claim predicate (`CLAIM_PREDICATE_KEYS`)

```
("predicate", "event_type")
```

## `_first_non_empty` semantics

| Input shape | Behavior |
|-------------|----------|
| First key absent | Skip; try next. |
| First key value is `None` | Treat as missing; try next. |
| First key value is `""` | Treat as missing; try next. |
| First key value is non-empty `str` | Return it. |
| First key value is `int` / `float` / `bool` / `bytes` / `dict` / `list` / `tuple` | `ValueError` — no silent `str()` coercion. |
| First key value is `str` containing an unpaired UTF-16 surrogate (`U+D800`–`U+DFFF` not part of a pair) | `ValueError` — caught at normalize boundary, before SQLite encode. |
| All keys exhausted | `ValueError` listing the alias tuple so the caller sees what was tried. |

Numeric `0`, the empty `dict`, the empty `list`, `False`, and `True` all
raise. The intent is to fail fast on caller bugs (passing the wrong kind of
value) rather than producing surprising rows.

## `_coerce_optional_float` semantics

| Input | Output |
|-------|--------|
| `None` | `None` |
| `int` (not `bool`) | `float(value)` |
| `float` | `value` |
| `bool` | `ValueError` (explicit reject — `bool` is `int` subclass; silent coercion masks bugs) |
| any other type | `ValueError` |

Used by `RealMemoryRouter.ingest` for `IngestRequest.payload['confidence']`
on claim payloads.

## Read-side DTO `body` field

Every hit returned by `RealMemoryRouter.query` carries a `body` key:

```python
hit = {
    "id": ...,
    "text": ...,        # mirrors h.title
    "body": ...,        # canonical, derived via alias precedence
    "created_at": ...,
    "source_id": ...,
    "kind": ...,
    "score": ...,
    "evidence": ...,
    "full": ...,
    "explain": ...,
}
```

`body` is **always a `str`** (never `None`). It is derived by trying the
hit's `full` payload first, then `evidence`, applying the kind-specific
alias tuple via `_first_non_empty`. If neither source resolves, `body`
falls back to the hit's `title` (or `""` if the title is also missing).

### Read-side leniency vs ingest strictness

Ingest raises on type errors. Read-side does NOT. Reasoning: a hit that
came back from a query already passed persistence (the ingest boundary has
already validated). If a legacy row predates the alias rules and lacks any
recognized body alias, returning `body=""` keeps the consumer contract
("`body` is always a `str`") rather than aborting the whole response.

## Flag-off behavior

The `body` field is **only present when the request was routed through
`RealMemoryRouter`**, i.e. under `MEMORY_ROUTER=true`. When the flag is
off, the legacy retrieve path returns hits without the canonical `body`
field.

Consumers (LongMemEval, future OAS-generated clients, scripts) MUST treat
`body` as **optional** in API specs and DTO consumers, even though it is
always present under router-on. Concretely:

```python
# Correct — works under both flag states
body = hit.get("body") or hit.get("text") or ""

# WRONG — KeyError when MEMORY_ROUTER=false
body = hit["body"]
```

The optionality is documented at the API spec level; runtime presence is
guaranteed only on the router-on code path.

## Acceptance / verification

- `tests/router/test_normalize.py` — 32 unit tests on the helpers.
- `tests/router/test_real_adapter_ingest.py` — 19 tests on ingest-side use.
- `tests/router/test_real_adapter_query.py` — DTO `body` contract tests.

Any change to alias precedence requires updating this file, the constants
in `parallax/router/real_adapter.py`, and the tests in tandem. The 4-reviewer
addendum baseline (signed 2026-04-25 in `prd.json.laned3.done`) is the
contractual reference.
