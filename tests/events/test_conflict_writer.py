"""Tests for parallax.events.conflict_writer (M3b Phase 2 — US-005).

The conflict-event writer emits an ``arbitration_conflict`` row to the
``events`` table whenever a :class:`LiveArbitrationDecision` would require
manual review (winning_source in {"tie","fallback"}). Unit-level coverage
focuses on:

- envelope schema correctness (6 fixed keys, sort_keys=True deterministic)
- idempotency dedup window (1h) keyed by (canonical_ref, conflict_field)
- DataQualityFlag enum: 3 values, str-typed, lower_snake_case
- canonical_ref derivation (primary first hit -> secondary first hit ->
  sentinel)
- best-effort fail-closed on write failure (returns "" — caller swallows)
- migration idempotency
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time

import pytest

from parallax.events.conflict_writer import (
    NO_CANONICAL_REF_SENTINEL,
    WriteFailure,
    write_conflict_event,
)
from parallax.migrations import migrate_to_latest
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.live_arbitration import LiveArbitrationDecision
from parallax.router.types import DataQualityFlag, QueryType
from parallax.sqlite_store import connect

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: pathlib.Path):
    db = tmp_path / "conflict_writer.db"
    c = connect(db)
    migrate_to_latest(c)
    try:
        yield c
    finally:
        c.close()


def _evidence(*ids: str) -> RetrievalEvidence:
    hits = tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids)
    return RetrievalEvidence(hits=hits, stages=("test",))


def _decision(
    *,
    winning_source: str = "fallback",
    correlation_id: str = "cid-1",
    tie_breaker_rule: str = "source-level",
    query_type: QueryType = QueryType.RECENT_CONTEXT,
    decided_at_us_utc: int | None = None,
) -> LiveArbitrationDecision:
    return LiveArbitrationDecision(
        winning_source=winning_source,  # type: ignore[arg-type]
        tie_breaker_rule=tie_breaker_rule,
        conflict_event_id=None,
        policy_version="v0.3.0-rc",
        correlation_id=correlation_id,
        query_type=query_type,
        reason_code=f"source-level/{query_type.value}/{winning_source}",
        decided_at_us_utc=(
            decided_at_us_utc if decided_at_us_utc is not None else time.time_ns() // 1_000
        ),
    )


def _payload(*, primary_ids=("p1",), secondary_ids=("s1",)) -> dict:
    return {
        "primary": _evidence(*primary_ids),
        "secondary": _evidence(*secondary_ids),
        "user_id": "u1",
    }


def _row_count(conn: sqlite3.Connection, *, event_type: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE event_type = ?", (event_type,)).fetchone()[
            0
        ]
    )


def _select_envelope(conn: sqlite3.Connection, event_id: str) -> dict:
    row = conn.execute("SELECT payload_json FROM events WHERE event_id = ?", (event_id,)).fetchone()
    assert row is not None, f"event {event_id!r} not in DB"
    return json.loads(row["payload_json"])


# ---------------------------------------------------------------------------
# DataQualityFlag enum (B)
# ---------------------------------------------------------------------------


def test_data_quality_flag_enum_three_values_str_typed() -> None:
    members = {
        DataQualityFlag.COLD_START,
        DataQualityFlag.CORPUS_IMMATURE,
        DataQualityFlag.NORMAL,
    }
    assert len(members) == 3
    assert DataQualityFlag.COLD_START.value == "cold_start"
    assert DataQualityFlag.CORPUS_IMMATURE.value == "corpus_immature"
    assert DataQualityFlag.NORMAL.value == "normal"
    # str-typed (StrEnum or str-subclass)
    assert isinstance(DataQualityFlag.COLD_START, str)
    # All values are lower_snake_case
    for v in DataQualityFlag:
        assert v.value == v.value.lower()
        assert " " not in v.value
        assert "-" not in v.value


# ---------------------------------------------------------------------------
# Envelope schema (A)
# ---------------------------------------------------------------------------


def test_envelope_schema_fields_present_and_deterministic(conn: sqlite3.Connection) -> None:
    decision = _decision(correlation_id="cid-A", winning_source="fallback")
    payload = _payload()
    eid = write_conflict_event(decision, payload, conn)
    assert eid != ""
    env = _select_envelope(conn, eid)

    assert env["event_type"] == "arbitration_conflict"
    assert env["correlation_id"] == "cid-A"
    assert env["schema_version"] == "1.0"
    assert isinstance(env["timestamp_us_utc"], int) and env["timestamp_us_utc"] > 0
    assert env["data_quality_flag"] == "cold_start"
    # payload is the parsed decision JSON line (round-trippable to a dict)
    assert isinstance(env["payload"], dict)
    assert env["payload"]["correlation_id"] == "cid-A"
    assert env["payload"]["winning_source"] == "fallback"
    # Stored payload is byte-deterministic JSON (sort_keys=True)
    raw = conn.execute("SELECT payload_json FROM events WHERE event_id = ?", (eid,)).fetchone()[
        "payload_json"
    ]
    assert raw == json.dumps(env, sort_keys=True)


def test_envelope_data_quality_flag_propagation(conn: sqlite3.Connection) -> None:
    decision = _decision(correlation_id="cid-DQ")
    eid = write_conflict_event(decision, _payload(), conn, data_quality_flag=DataQualityFlag.NORMAL)
    env = _select_envelope(conn, eid)
    assert env["data_quality_flag"] == "normal"


def test_envelope_empty_payload_dict_does_not_crash(conn: sqlite3.Connection) -> None:
    decision = _decision(correlation_id="cid-empty")
    eid = write_conflict_event(decision, {}, conn)
    assert eid != ""
    env = _select_envelope(conn, eid)
    assert env["event_type"] == "arbitration_conflict"


# ---------------------------------------------------------------------------
# canonical_ref derivation
# ---------------------------------------------------------------------------


def test_canonical_ref_uses_primary_first_hit_when_present(conn: sqlite3.Connection) -> None:
    decision = _decision(correlation_id="cid-prim")
    payload = {
        "primary": _evidence("PRIM-1", "PRIM-2"),
        "secondary": _evidence("SEC-1"),
    }
    eid_1 = write_conflict_event(decision, payload, conn)
    # Re-call within the dedup window with a *different* correlation id but
    # the same canonical_ref + conflict_field — should dedup, returning eid_1.
    decision_dup = _decision(correlation_id="cid-prim-dup")
    eid_2 = write_conflict_event(decision_dup, payload, conn)
    assert eid_1 == eid_2


def test_canonical_ref_falls_back_to_secondary_when_primary_empty(
    conn: sqlite3.Connection,
) -> None:
    decision = _decision(correlation_id="cid-sec")
    # Primary empty, secondary populated — canonical_ref derived from secondary
    payload_a = {"primary": _evidence(), "secondary": _evidence("SEC-X")}
    eid_a = write_conflict_event(decision, payload_a, conn)
    # Same canonical_ref + same conflict_field → dedup
    payload_b = {"primary": _evidence(), "secondary": _evidence("SEC-X")}
    eid_b = write_conflict_event(_decision(correlation_id="cid-sec-2"), payload_b, conn)
    assert eid_a == eid_b


def test_canonical_ref_sentinel_when_both_sides_empty(conn: sqlite3.Connection) -> None:
    decision = _decision(correlation_id="cid-none")
    payload = {"primary": _evidence(), "secondary": _evidence()}
    eid = write_conflict_event(decision, payload, conn)
    _ = _select_envelope(conn, eid)
    # Sentinel must be a stable, documented string; we expose it on the
    # writer module so tests stay coupled to the public constant.
    assert NO_CANONICAL_REF_SENTINEL == "__no_canonical_ref__"
    # Round-trip: the dedup row exists for that sentinel
    payload_dup = {"primary": _evidence(), "secondary": _evidence()}
    eid_dup = write_conflict_event(_decision(correlation_id="cid-none-dup"), payload_dup, conn)
    assert eid == eid_dup


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_within_1h_returns_existing_event_id(conn: sqlite3.Connection) -> None:
    """Two calls with same (canonical_ref, conflict_field) within 1h → 1 row."""
    decision_1 = _decision(correlation_id="cid-idem-1")
    decision_2 = _decision(correlation_id="cid-idem-2")
    payload = _payload(primary_ids=("P-IDEM",))
    eid_1 = write_conflict_event(decision_1, payload, conn)
    eid_2 = write_conflict_event(decision_2, payload, conn)
    assert eid_1 == eid_2
    assert _row_count(conn, event_type="arbitration_conflict") == 1


def test_idempotency_different_conflict_field_inserts_separate_rows(
    conn: sqlite3.Connection,
) -> None:
    """Same canonical_ref, *different* tie_breaker_rule → two distinct rows."""
    payload = _payload(primary_ids=("P-MULTI",))
    eid_1 = write_conflict_event(
        _decision(correlation_id="cid-r1", tie_breaker_rule="source-level"),
        payload,
        conn,
    )
    eid_2 = write_conflict_event(
        _decision(correlation_id="cid-r2", tie_breaker_rule="other-rule"),
        payload,
        conn,
    )
    assert eid_1 != eid_2
    assert _row_count(conn, event_type="arbitration_conflict") == 2


def test_idempotency_outside_1h_window_inserts_new_row(conn: sqlite3.Connection) -> None:
    """Two calls > 1h apart → 2 separate rows.

    Trick: pre-insert a fake row with a created_at older than 1h, then call
    write_conflict_event with the same (canonical_ref, conflict_field) and
    verify a new row appears.
    """
    payload = _payload(primary_ids=("P-OLD",))
    decision_1 = _decision(correlation_id="cid-old", tie_breaker_rule="source-level")
    eid_1 = write_conflict_event(decision_1, payload, conn)

    # Manually rewind the created_at on the existing row so it falls outside
    # the dedup window. Use the trigger-bypassing approach: events are
    # append-only by trigger; instead we simulate "old" by passing
    # now=<future> on the second call.
    now_us = time.time_ns() // 1_000
    far_future_us = now_us + 3700 * 1_000_000  # > 1h later

    eid_2 = write_conflict_event(
        _decision(correlation_id="cid-new", tie_breaker_rule="source-level"),
        payload,
        conn,
        now_us_utc=far_future_us,
    )
    assert eid_1 != eid_2
    assert _row_count(conn, event_type="arbitration_conflict") == 2


def test_idempotency_counter_increments_on_dedup_hit(conn: sqlite3.Connection) -> None:
    """Each dedup hit must increment the deduped counter (visible to caller)."""
    from parallax.events import conflict_writer as cw_mod

    cw_mod._dedup_hits["count"] = 0  # internal counter — KISS, single-process
    payload = _payload(primary_ids=("P-COUNT",))
    write_conflict_event(_decision(correlation_id="cid-c1"), payload, conn)
    write_conflict_event(_decision(correlation_id="cid-c2"), payload, conn)
    write_conflict_event(_decision(correlation_id="cid-c3"), payload, conn)
    assert cw_mod.get_dedup_hit_count() == 2  # first call inserts; 2 dedup hits


# ---------------------------------------------------------------------------
# Best-effort write failure
# ---------------------------------------------------------------------------


def test_write_failure_returns_empty_string(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the INSERT fails, write_conflict_event returns "" (best-effort)."""
    decision = _decision(correlation_id="cid-fail")

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated write failure")

    # Patch the insert helper used internally
    from parallax.events import conflict_writer as cw_mod

    monkeypatch.setattr(cw_mod, "_insert_event_row", _boom)
    eid = write_conflict_event(decision, _payload(), conn)
    assert eid == ""


def test_write_failure_class_is_exposed() -> None:
    """The WriteFailure sentinel exception class is exported for callers
    that need to introspect failures from telemetry hooks (we never raise it
    out of write_conflict_event — fail-closed — but the symbol is part of
    the public surface)."""
    assert issubclass(WriteFailure, Exception)


# ---------------------------------------------------------------------------
# H4 — write-failure counter (separate from dedup hit counter)
# ---------------------------------------------------------------------------


def test_write_failure_increments_counter(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story H4 — when _insert_event_row raises, the write-failure counter
    must increment from 0 to 1 BEFORE write_conflict_event returns ''."""
    from parallax.events import conflict_writer as cw_mod

    cw_mod.reset_write_failure_count()
    assert cw_mod.get_write_failure_count() == 0

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated write failure")

    monkeypatch.setattr(cw_mod, "_insert_event_row", _boom)
    eid = write_conflict_event(_decision(correlation_id="cid-h4-w"), _payload(), conn)
    assert eid == ""
    assert cw_mod.get_write_failure_count() == 1


def test_select_failure_increments_counter(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story H4 — failures inside the dedup SELECT must also increment the
    write-failure counter (any caught exception increments)."""
    from parallax.events import conflict_writer as cw_mod

    cw_mod.reset_write_failure_count()

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated select failure")

    monkeypatch.setattr(cw_mod, "_select_existing_event_id", _boom)
    eid = write_conflict_event(_decision(correlation_id="cid-h4-s"), _payload(), conn)
    assert eid == ""
    assert cw_mod.get_write_failure_count() == 1


def test_reset_write_failure_count_zeroes(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story H4 — reset_write_failure_count returns the counter to zero."""
    from parallax.events import conflict_writer as cw_mod

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated write failure")

    monkeypatch.setattr(cw_mod, "_insert_event_row", _boom)
    write_conflict_event(_decision(correlation_id="cid-h4-r"), _payload(), conn)
    assert cw_mod.get_write_failure_count() >= 1
    cw_mod.reset_write_failure_count()
    assert cw_mod.get_write_failure_count() == 0


# ---------------------------------------------------------------------------
# MED-USER-ID-SENTINEL — '__system__' fallback for missing user_id
# ---------------------------------------------------------------------------


def test_no_user_id_uses_system_sentinel(conn: sqlite3.Connection) -> None:
    """Story MED-USER-ID-SENTINEL — when payload omits user_id, the row's
    user_id column is set to the documented '__system__' sentinel."""
    from parallax.events.conflict_writer import CONFLICT_EVENT_SYSTEM_USER_ID

    payload_no_uid = {
        "primary": _evidence("p-sentinel"),
        "secondary": _evidence("p-sentinel"),
    }
    eid = write_conflict_event(_decision(correlation_id="cid-sentinel"), payload_no_uid, conn)
    assert eid != ""
    row = conn.execute("SELECT user_id FROM events WHERE event_id = ?", (eid,)).fetchone()
    assert row["user_id"] == CONFLICT_EVENT_SYSTEM_USER_ID
    assert CONFLICT_EVENT_SYSTEM_USER_ID == "__system__"


# ---------------------------------------------------------------------------
# Migration idempotency (C)
# ---------------------------------------------------------------------------


def test_migration_idempotent_repeated_runs(tmp_path: pathlib.Path) -> None:
    """Running migrate_to_latest twice must not error and must leave schema unchanged."""
    db = tmp_path / "mig_idem.db"
    c = connect(db)
    try:
        migrate_to_latest(c)
        before = sorted(
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            ).fetchall()
        )
        # Second call is a no-op (everything in schema_migrations already)
        migrate_to_latest(c)
        after = sorted(
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            ).fetchall()
        )
        assert before == after
        # The new conflict-event index exists exactly once.
        assert sum(1 for n in after if "events_event_type_target_id" in n) == 1
    finally:
        c.close()


def test_migration_creates_event_type_correlation_id_index(
    tmp_path: pathlib.Path,
) -> None:
    db = tmp_path / "mig_idx.db"
    c = connect(db)
    try:
        migrate_to_latest(c)
        idx = c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='events' "
            "AND name = 'idx_events_event_type_target_id'"
        ).fetchone()
        assert idx is not None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# H1 — dedup SELECT scaling: created_at filter + LIMIT 1 + index usage
# ---------------------------------------------------------------------------


def test_dedup_select_uses_index(conn: sqlite3.Connection) -> None:
    """The dedup SELECT must hit the (event_type, target_id) index, not scan.

    Story H1 — the original SELECT had no created_at filter and no LIMIT, so
    EXPLAIN QUERY PLAN would emit a SCAN over all matching rows. Adding the
    bound created_at parameter + LIMIT 1 forces SQLite's planner to use the
    composite index keyed on (event_type, target_id) for an O(log n) lookup.
    """
    # The dedup SELECT now mirrors what _select_existing_event_id issues.
    plan_rows = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT event_id, payload_json FROM events "
        "WHERE event_type = ? "
        "AND target_id = ? "
        "AND approval_tier = ? "
        "AND created_at >= ? "
        "ORDER BY created_at DESC "
        "LIMIT 1",
        ("arbitration_conflict", "test-canonical", "source-level", "1970-01-01T00:00:00Z"),
    ).fetchall()
    plan_text = "\n".join(str(row[3]) if len(row) > 3 else str(row) for row in plan_rows)
    assert (
        "USING INDEX" in plan_text or "USING COVERING INDEX" in plan_text
    ), f"Expected dedup SELECT to use an index. Got plan:\n{plan_text}"


def test_write_conflict_event_works_with_default_row_factory(tmp_path: pathlib.Path) -> None:
    """Story H2 — vanilla sqlite3.connect (no row_factory) must still work.

    The dedup SELECT uses Mapping access (``row["payload_json"]``); the
    writer must self-set ``conn.row_factory = sqlite3.Row`` so callers
    that pass a vanilla connection don't trip TypeError on tuple indexing.
    """
    db = tmp_path / "default_factory.db"
    raw = sqlite3.connect(str(db))
    # Make sure factory is the default (None)
    raw.row_factory = None
    migrate_to_latest(raw)

    try:
        # First call — inserts a row through the dedup-then-insert path.
        eid_1 = write_conflict_event(_decision(correlation_id="cid-rf-1"), _payload(), raw)
        assert eid_1 != ""
        # Second call within 1h window — must hit the dedup SELECT
        # (Mapping access on the row); without row_factory enforcement this
        # would TypeError and the writer would swallow it and return "".
        eid_2 = write_conflict_event(_decision(correlation_id="cid-rf-2"), _payload(), raw)
        assert eid_1 == eid_2, "dedup SELECT must succeed even with default row_factory"
    finally:
        raw.close()


def test_caller_row_factory_preserved(tmp_path: pathlib.Path) -> None:
    """Story H2 — the writer must restore the caller's row_factory on exit."""
    db = tmp_path / "preserve_factory.db"
    raw = sqlite3.connect(str(db))
    migrate_to_latest(raw)

    def custom_factory(cursor, row):  # type: ignore[no-untyped-def]
        return list(row)  # distinct from sqlite3.Row + tuple

    raw.row_factory = custom_factory
    try:
        write_conflict_event(_decision(correlation_id="cid-rf-pres"), _payload(), raw)
        assert (
            raw.row_factory is custom_factory
        ), "caller's row_factory must be restored after write_conflict_event"
    finally:
        raw.close()


def test_dedup_select_filters_by_created_at_in_sql(conn: sqlite3.Connection) -> None:
    """The dedup window check must filter created_at in the SQL WHERE clause.

    Story H1 — proves the SQL contains the AND created_at >= ? bound
    parameter so old rows do not flow through Python before being filtered.

    Approach: wrap the real connection in a thin proxy that captures every
    ``execute`` call's SQL, hand the proxy to ``_select_existing_event_id``,
    and assert the captured SELECT contains the new clause + LIMIT.
    """
    from parallax.events import conflict_writer as cw_mod

    captured: list[str] = []

    class _CapturingConn:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, sql, *args, **kwargs):
            captured.append(sql)
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str):
            return getattr(self._real, name)

    proxy = _CapturingConn(conn)
    cw_mod._select_existing_event_id(
        proxy,  # type: ignore[arg-type]
        canonical_ref="anything",
        conflict_field="source-level",
        window_start_us=0,
    )

    select_sqls = [s for s in captured if "SELECT event_id, payload_json FROM events" in s]
    assert select_sqls, "expected _select_existing_event_id to issue the dedup SELECT"
    assert any(
        "created_at >= ?" in s for s in select_sqls
    ), f"dedup SELECT missing created_at filter: {select_sqls!r}"
    assert any(
        "LIMIT 1" in s for s in select_sqls
    ), f"dedup SELECT missing LIMIT 1: {select_sqls!r}"
