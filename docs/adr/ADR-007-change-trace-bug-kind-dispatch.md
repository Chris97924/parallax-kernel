# ADR-007: CHANGE_TRACE bug-kind dispatch via payload disambiguation

## Status

Accepted — 2026-04-23

## Context

Lane D-1 (PR #2, commit `20e5a4a`) froze `QueryType` as a 5-value closed set:
`RECENT_CONTEXT`, `ARTIFACT_CONTEXT`, `ENTITY_PROFILE`, `CHANGE_TRACE`,
`TEMPORAL_CONTEXT`. The pre-router retrieval API had six `RetrieveKind`
values (`recent`, `file`, `decision`, `bug`, `entity`, `timeline`).
`CROSSWALK_SEED` (`parallax/router/crosswalk_seed.py:25-26`) collapses both
`RetrieveKind.decision` AND `RetrieveKind.bug` to `QueryType.CHANGE_TRACE`:

```python
"RetrieveKind.decision": QueryType.CHANGE_TRACE,
"RetrieveKind.bug":      QueryType.CHANGE_TRACE,
```

`RealMemoryRouter` (Lane D-2, `parallax/router/real_adapter.py:31`) currently
dispatches `CHANGE_TRACE → parallax.retrieve.by_decision` only, silently
losing the `by_bug_fix` retrieval path. If `by_bug_fix` was contributing
measurable recall on bug-related LongMemEval questions in v0.5, the merge
is empirically lossy.

2026-04-22 xcouncil Round 2 voted 5/8 to keep the 5-value closed set. The
Sonnet critic warned that `bug` retrieval may contribute measurable
LongMemEval scoring on specific bug-related questions — merging without
empirical validation risks scoring regression. 2026-04-23 morning C-route
cleanup removed the test
`test_bug_kind_still_returns_results_when_router_enabled` because it had
been written under the Option B assumption (add 6th enum); the test is
deliberately deferred until this ADR lands.

## Decision

Keep the 5-value `QueryType` frozen. Disambiguate `decision` vs `bug`
dispatch at the **payload level**: `QueryRequest.params` carries a
`legacy_kind` hint preserved from the originating `RetrieveKind`.
`RealMemoryRouter.query` inspects `legacy_kind` and dispatches within the
`CHANGE_TRACE` branch to either `parallax.retrieve.by_decision` (default)
or `parallax.retrieve.by_bug_fix` (when `legacy_kind == "bug"`).

- `CROSSWALK_SEED` unchanged (both `decision` and `bug` → `CHANGE_TRACE`).
- `QueryRequest.params` contract: include `legacy_kind: str | None`.
- `RealMemoryRouter.query` for `CHANGE_TRACE`:
  - `params.legacy_kind == "bug"` → `parallax.retrieve.by_bug_fix`
  - otherwise → `parallax.retrieve.by_decision`
- Legacy-to-router adapter shim populates `legacy_kind` with the original
  RetrieveKind string (`"decision"`, `"bug"`, ...).

## Alternatives considered

### Option A — 410 Gone (`by_bug_fix` removed)

Dispatch `CHANGE_TRACE` to `by_decision` unconditionally; drop `by_bug_fix`.

Rejected: silently loses retrieval signal; regression risk unquantified;
contradicts Sonnet critic's LongMemEval warning. Clean but too aggressive
given v0.5 empirical unknowns.

### Option B — Add `QueryType.CHANGE_TRACE_BUG` as 6th enum

Expand closed set to six values so bug-fix retrieval has its own
first-class query type.

Rejected: breaks D-1 contract freeze; requires D-1 re-ship + schema
migration + downstream caller migration; xcouncil Round 2 voted 5/8
against. The entire point of D-1 was to freeze the closed set before
downstream work commits to its shape.

### Option C — Split `CHANGE_TRACE` into `CHANGE_TRACE_DECISION` + `CHANGE_TRACE_BUG`

Replace single `CHANGE_TRACE` with two named variants.

Rejected: same D-1 freeze violation as Option B, plus forces every
future caller to disambiguate between two nearly-identical types at the
type level. Over-engineered for a single sub-variant.

### Option D — Payload-level disambiguation (chosen)

Same `QueryType.CHANGE_TRACE`; sub-dispatch inside the router via
`QueryRequest.params`.

Chosen: preserves D-1 freeze; preserves bug-fix retrieval path; remains
empirically testable because `by_decision` and `by_bug_fix` both still
live in `parallax.retrieve`.

## Consequences

**Positive**
- D-1 5-value `QueryType` freeze preserved; no D-1 re-ship or schema
  migration required.
- `by_bug_fix` retrieval path preserved through Lane D-2/D-3 adapter.
- Empirically testable: both retrieval paths continue to exist, allowing
  side-by-side LongMemEval comparison.
- Future retrieval variants within `CHANGE_TRACE` can use the same
  `legacy_kind` hook without another ADR.

**Negative**
- `CHANGE_TRACE` now has two implicit sub-dispatch paths. Router
  implementation is slightly more complex; care needed in diagnostic
  logs to distinguish which path served a query.
- Callers that do not populate `params.legacy_kind` silently fall back
  to `by_decision`. Observability gap: we cannot tell whether a query
  "wanted" bug-fix semantics and missed.

## Acceptance criteria (for Lane D-3)

**AC-1 (dispatch semantics).** `RealMemoryRouter.query` with
`QueryType.CHANGE_TRACE` and `params.legacy_kind == "bug"` MUST invoke
`parallax.retrieve.by_bug_fix`. With `legacy_kind == "decision"` or
absent, it MUST invoke `parallax.retrieve.by_decision`. Round-trip test
in `tests/router/test_real_adapter.py` named
`test_change_trace_bug_kind_dispatches_to_by_bug_fix`.

**AC-2 (docstring).** `parallax/router/crosswalk_seed.py` docstring MUST
state that `RetrieveKind.decision` and `RetrieveKind.bug` both map to
`CHANGE_TRACE` and that downstream dispatch is disambiguated via
`QueryRequest.params.legacy_kind`. ADR-007 cited by filename.

**AC-3 (LongMemEval empirical gate — blocks `MEMORY_ROUTER=true`
default-on).** Before flipping `MEMORY_ROUTER=true` as the default, run
the bug-correlated LongMemEval subset through the new router path (via
the `legacy_kind="bug"` input) and compare e2e accuracy against the
v0.5 `by_bug_fix` baseline. Regression tolerance: **−2 percentage
points max**. If regression exceeds 2pp, open a follow-up ADR re-opening
the choice set (B and C become eligible again).

**AC-4 (no silent path).** When `legacy_kind` is `None` on a
`CHANGE_TRACE` query, emit a debug log entry marking "legacy_kind
absent, default by_decision". Makes the observability gap explicit.

## Implementation hints (Lane D-3)

- `US-D3-01` (`RealMemoryRouter.ingest`): no change from this ADR; ADR
  is query-side.
- `US-D3-04` (field normalization canonical evidence field): when the
  legacy-API shim translates `retrieve.by_bug_fix(...)` into a
  `QueryRequest`, it must set `params["legacy_kind"] = "bug"`. Same for
  `decision`, `file`, etc. (non-bug inputs optional but recommended for
  symmetry / observability).
- Reshape the test deleted on 2026-04-23
  (`test_bug_kind_still_returns_results_when_router_enabled`): rewrite
  it to pass `legacy_kind="bug"` and assert results come from the
  `by_bug_fix` code path, not simply "any results returned".
- Update `prd.json.laned3.draft` to cite ADR-007 under `US-D3-07`.

## References

- Lane D-1 contract freeze (PR #2, commit `20e5a4a`).
- Lane D-2 real adapter (PR #3, commits `76699da` `aac937c` `a8b88d5`
  `8adf311` `6cec710` `e328f37` `3501f33`).
- `parallax/router/crosswalk_seed.py:25-26` — seed mapping for
  `decision` + `bug` → `CHANGE_TRACE`.
- `parallax/router/real_adapter.py:27-35` — current `_DISPATCH` table
  that this ADR refines.
- 2026-04-22 xcouncil Round 2 vote: 5/8 for 5-value retention; Sonnet
  critic warning on LongMemEval bug-subset scoring.
- 2026-04-23 morning C-route cleanup — deletion of
  `test_bug_kind_still_returns_results_when_router_enabled` (to be
  reshaped after this ADR lands).
