# ADR-003 — `events.target_kind` unconstrained vs `decisions.target_kind` hard CHECK

- **Status:** Accepted
- **Date:** 2026-04-18
- **Frozen in:** v0.1.3

## Context

Two tables in the Parallax schema carry a `target_kind` column:

- `decisions.target_kind` — which entity the decision acts on
  (e.g., "this decision confirms claim X").
- `events.target_kind` — which entity an audit-log row describes
  (e.g., "this event records that decision X transitioned state").

In v0.1.2 both columns were documented only by a trailing comment,
and the validator module legalized a unified
`VALID_TARGET_KINDS = {memory, claim, source, decision}`. The 2-agent
review on v0.1.2 flagged two problems:

1. **`decisions.target_kind` had no DB-level enforcement.** The
   "valid values" lived only in a comment, so a buggy writer could
   insert `target_kind = 'decision'` on a decision row without any
   constraint firing. The comment said otherwise; reviewers would
   believe the comment. Discipline-only enforcement is the kind of
   rule that fails under pressure, not the kind that prevents bugs.
2. **A symmetric `VALID_TARGET_KINDS` hid the asymmetry.** The
   validator exposed one allow-list, but the two columns genuinely
   want different allow-lists: `events` must be able to log
   decision-level state changes, and `decisions` must not point at
   other decisions in the Phase-0 model. A single frozenset cannot
   express both.

## Decision

The two columns are deliberately asymmetric, and the asymmetry is
enforced at two layers — DB and code — so that neither can drift
from the other.

**Schema (`schema.sql`):**

- `decisions.target_kind TEXT NOT NULL CHECK (target_kind IN ('claim','memory','source'))`
  — hard CHECK. Decisions never target other decisions at the DB
  level; inserting `target_kind='decision'` raises a CHECK-constraint
  error, not a quiet business-logic fault.
- `events.target_kind TEXT` — no CHECK, and that absence is
  deliberate. An event row must be able to record
  `event_type='decision.state_changed'` with
  `target_kind='decision', target_id=<decision_id>`; a symmetric
  CHECK would make this legitimate audit row illegal.

**Validators (`parallax/validators.py`):**

- `VALID_TARGET_KINDS: frozenset[str] = frozenset({"memory", "claim", "source", "decision"})`
  — the events-wide allow-list used by `target_ref_exists(conn, kind, id)`.
  Mirrors the (intentional) absence of a CHECK on `events.target_kind`.
- `DECISION_TARGET_KINDS: frozenset[str] = frozenset({"memory", "claim", "source"})`
  — the narrower allow-list that mirrors the `decisions.target_kind`
  CHECK. Decision writers MUST validate against this set, not
  against `VALID_TARGET_KINDS`.
- `TargetKind = Literal["memory","claim","source","decision"]` —
  the full type-level surface, re-exported from `parallax/__init__.py`
  alongside both frozensets.

The pairing is asymmetric by design: `VALID_TARGET_KINDS` has a
consumer (`target_ref_exists`) that events writers can call;
`DECISION_TARGET_KINDS` has no validator helper in v0.1.3 and is
enforced DB-side only. The first decision writer must either (a) check
membership against `DECISION_TARGET_KINDS` explicitly or (b) rely on
the `CHECK` to surface `sqlite3.IntegrityError`, and the ADR for that
writer should decide which.

## Consequences

- **Cross-entity invariant (flagged in `docs/state-transitions.md`):**
  `decisions.target_kind ⊂ events.target_kind`. Reviewers who see the
  two allow-lists in isolation will be tempted to "clean up" the
  asymmetry by unifying them. They must not. The asymmetry is the
  design; it is what makes the audit log total while keeping the
  decisions table tight.
- Future decision writers land against a stable contract:
  `DECISION_TARGET_KINDS` is the allow-list; violating it is a
  `CHECK` failure at insert time regardless of what the Python layer
  does. Forgetting a validator check is caught at the DB boundary.
- The `events` writer — when it lands — must accept
  `target_kind='decision'`. Any future CHECK added to
  `events.target_kind` has to include `'decision'`; a narrower CHECK
  would make decision-level audit rows illegal and is a new ADR, not
  an in-place edit to this one.
- Test coverage: v0.1.3 validator tests assert both frozensets
  explicitly, and `tests/test_schema.py` asserts at the DB layer that
  inserting `decisions.target_kind='decision'` raises
  `sqlite3.IntegrityError` while `events.target_kind='decision'`
  inserts cleanly.

## Alternatives considered

**Symmetric CHECK on both columns** — rejected. If
`events.target_kind` also restricted to `{'memory', 'claim', 'source'}`,
the audit log could not record a legitimate `decision.state_changed`
row without violating the constraint. Either the audit log loses
decision-level traceability, or every decision-state-change write
has to hand-roll an escape hatch. Both options defeat the audit-log
contract that `events` can witness any entity's state transition.

**No CHECK on either column** — rejected. The v0.1.2 status quo.
Without a DB-level CHECK on `decisions.target_kind`, a buggy writer
inserting `target_kind='decision'` (or `'foo'`) produces a row that
looks valid on read and only fails later, downstream, when something
tries to dereference the target. Decisions are first-class write
targets — recursive-decision bugs would ship. Discipline-only
enforcement is fragile; the CHECK is cheap and closes the gap.

**Single `VALID_TARGET_KINDS` plus a second code-level check on
decision writers** — rejected. This was effectively the v0.1.2 shape.
It works right up until the day a decision writer forgets the extra
check (a pure Python-layer rule with no DB backstop). Two frozensets
and a DB CHECK make the asymmetry impossible to forget: the narrower
set is called out by name, and violating it fails at the boundary.

**Moving decisions into events entirely (no separate table)** —
rejected, out of scope for v0.1.x. This is a much larger
architectural question (are decisions first-class entities with
their own state machine, or are they just events?); revisit when
the first decision writer lands, not here.

## References

- `schema.sql` — `decisions` table: `target_kind ... CHECK (target_kind IN ('claim','memory','source'))`.
- `schema.sql` — `events` table: `target_kind TEXT` (no CHECK; see
  inline comment).
- `parallax/validators.py` — `VALID_TARGET_KINDS`,
  `DECISION_TARGET_KINDS`, `TargetKind`, `target_ref_exists`.
- `parallax/__init__.py` — re-exports both frozensets and
  `TargetKind`.
- `docs/state-transitions.md`, *Cross-entity invariants* — records
  the `decisions.target_kind ⊂ events.target_kind` invariant for
  future reviewers.
- `CHANGELOG.md`, `[0.1.3] > Fixed` and `[0.1.3] > Added` — entries
  covering the CHECK addition and the `DECISION_TARGET_KINDS`
  frozenset export.
- `tests/test_validators.py` — asserts both frozensets explicitly.
- `tests/test_schema.py` —
  `TestDecisionsTargetKindCheck::test_decision_target_kind_rejected`
  and `TestEventsTargetKindUnconstrained::test_events_accepts_decision_target_kind`
  pin the DB-level asymmetry.
