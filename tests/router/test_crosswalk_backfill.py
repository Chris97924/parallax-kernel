"""M3-T0: Tests for crosswalk_backfill — bounded backfill + lazy populate.

ralplan reference: ralplan-m3-l2-dualread-2026-04-27.md §3 M3-T0, §10 Q11.

Coverage target: ≥80% of parallax.router.crosswalk_backfill.
"""

from __future__ import annotations

import sqlite3

import prometheus_client
import pytest

from parallax.ingest import ingest_claim, ingest_memory
from parallax.router.crosswalk_backfill import (
    BACKFILL_BATCH_LIMIT_DEFAULT,
    BackfillStats,
    backfill_crosswalk,
    lazy_materialize_by_content_hash,
    record_orphan_miss,
)

# `conn` fixture is provided by tests/conftest.py (fresh migrated sqlite3 db).

_USER = "alice"
_USER_B = "bob"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_memories(conn: sqlite3.Connection, user_id: str, count: int) -> list[str]:
    """Insert *count* unique memory rows; return list of memory_id strings."""
    ids: list[str] = []
    for i in range(count):
        result = ingest_memory(
            conn,
            user_id=user_id,
            title=f"title-{user_id}-{i}",
            summary=f"summary-{user_id}-{i}",
            vault_path=f"path/{user_id}/{i}.md",
        )
        ids.append(str(result))
    return ids


def _add_claims(conn: sqlite3.Connection, user_id: str, count: int) -> list[str]:
    """Insert *count* unique claim rows; return list of claim objects."""
    ids: list[str] = []
    for i in range(count):
        result = ingest_claim(
            conn,
            user_id=user_id,
            subject=f"subject-{i}",
            predicate=f"decision:choose-{i}",
            object_=f"object-{i}",
        )
        ids.append(str(result))
    return ids


def _crosswalk_count(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM crosswalk WHERE user_id = ?", (user_id,)).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_backfill_batch_limit_default_value() -> None:
    """BACKFILL_BATCH_LIMIT_DEFAULT is 10000 as specified."""
    assert BACKFILL_BATCH_LIMIT_DEFAULT == 10_000


def test_backfill_empty_corpus(conn: sqlite3.Connection) -> None:
    """Empty memory+claim tables → BackfillStats with zero counts."""
    stats = backfill_crosswalk(conn, user_id=_USER)
    assert isinstance(stats, BackfillStats)
    assert stats.rows_examined == 0
    assert stats.rows_inserted == 0
    assert stats.rows_skipped_existing == 0
    assert stats.batch_limit_reached is False


def test_backfill_inserts_memory_rows(conn: sqlite3.Connection) -> None:
    """5 memory rows → 5 crosswalk rows with parallax_target_kind='memory'."""
    _add_memories(conn, _USER, 5)
    stats = backfill_crosswalk(conn, user_id=_USER)
    assert stats.rows_inserted == 5
    assert stats.rows_examined == 5
    kinds = conn.execute(
        "SELECT DISTINCT parallax_target_kind FROM crosswalk WHERE user_id = ?",
        (_USER,),
    ).fetchall()
    assert len(kinds) == 1
    assert kinds[0][0] == "memory"


def test_backfill_inserts_claim_rows(conn: sqlite3.Connection) -> None:
    """5 claim rows → 5 crosswalk rows with parallax_target_kind='claim'."""
    _add_claims(conn, _USER, 5)
    stats = backfill_crosswalk(conn, user_id=_USER)
    assert stats.rows_inserted == 5
    assert stats.rows_examined == 5
    kinds = conn.execute(
        "SELECT DISTINCT parallax_target_kind FROM crosswalk WHERE user_id = ?",
        (_USER,),
    ).fetchall()
    assert len(kinds) == 1
    assert kinds[0][0] == "claim"


def test_backfill_idempotent(conn: sqlite3.Connection) -> None:
    """Second run returns rows_inserted=0, rows_skipped_existing=N."""
    _add_memories(conn, _USER, 3)
    _add_claims(conn, _USER, 2)
    stats1 = backfill_crosswalk(conn, user_id=_USER)
    assert stats1.rows_inserted == 5

    stats2 = backfill_crosswalk(conn, user_id=_USER)
    assert stats2.rows_inserted == 0
    assert stats2.rows_skipped_existing == 5


def test_backfill_respects_batch_limit_arg(conn: sqlite3.Connection) -> None:
    """batch_limit=3 → rows_examined==3, batch_limit_reached==True."""
    _add_memories(conn, _USER, 5)
    stats = backfill_crosswalk(conn, user_id=_USER, batch_limit=3)
    assert stats.rows_examined == 3
    assert stats.batch_limit_reached is True


def test_backfill_respects_batch_limit_env(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env CROSSWALK_BACKFILL_BATCH_LIMIT=2 caps at 2 rows."""
    monkeypatch.setenv("CROSSWALK_BACKFILL_BATCH_LIMIT", "2")
    _add_memories(conn, _USER, 5)
    stats = backfill_crosswalk(conn, user_id=_USER)
    assert stats.rows_examined == 2
    assert stats.batch_limit_reached is True


def test_backfill_user_isolation(conn: sqlite3.Connection) -> None:
    """Backfill for alice only inserts alice's rows; bob's rows untouched."""
    _add_memories(conn, _USER, 3)
    _add_memories(conn, _USER_B, 3)
    stats = backfill_crosswalk(conn, user_id=_USER)
    assert stats.rows_inserted == 3
    assert _crosswalk_count(conn, _USER) == 3
    assert _crosswalk_count(conn, _USER_B) == 0


def test_lazy_materialize_hit(conn: sqlite3.Connection) -> None:
    """Row exists with content_hash X → returns its canonical_ref."""
    _add_memories(conn, _USER, 1)
    backfill_crosswalk(conn, user_id=_USER)
    # Fetch the content_hash of the inserted crosswalk row.
    row = conn.execute(
        "SELECT canonical_ref, content_hash FROM crosswalk WHERE user_id = ?",
        (_USER,),
    ).fetchone()
    assert row is not None
    canonical_ref, content_hash = row[0], row[1]

    result = lazy_materialize_by_content_hash(conn, user_id=_USER, content_hash=content_hash)
    assert result == canonical_ref


def test_lazy_materialize_miss(conn: sqlite3.Connection) -> None:
    """content_hash not present in crosswalk → returns None."""
    result = lazy_materialize_by_content_hash(
        conn, user_id=_USER, content_hash="nonexistent-hash-abc123"
    )
    assert result is None


def test_record_orphan_miss_increments_counter(conn: sqlite3.Connection) -> None:
    """Calling record_orphan_miss N times increments the counter by N."""
    n = 4
    # Read current value before incrementing.
    before = _get_orphan_counter_value(_USER)
    for _ in range(n):
        record_orphan_miss(user_id=_USER)
    after = _get_orphan_counter_value(_USER)
    assert after - before == n


def test_record_orphan_miss_user_label_isolation(conn: sqlite3.Connection) -> None:
    """Two distinct user_ids have independent counter series."""
    before_alice = _get_orphan_counter_value(_USER)
    before_bob = _get_orphan_counter_value(_USER_B)
    record_orphan_miss(user_id=_USER)
    record_orphan_miss(user_id=_USER)
    record_orphan_miss(user_id=_USER_B)
    assert _get_orphan_counter_value(_USER) - before_alice == 2
    assert _get_orphan_counter_value(_USER_B) - before_bob == 1


def test_no_aphelion_call_during_backfill() -> None:
    """The module must not import or reference any Aphelion module."""
    import ast
    import pathlib

    src = (
        pathlib.Path(__file__).parent.parent.parent
        / "parallax"
        / "router"
        / "crosswalk_backfill.py"
    )
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert (
                "aphelion" not in module.lower()
            ), f"crosswalk_backfill.py imports from aphelion module: {module!r}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert (
                    "aphelion" not in alias.name.lower()
                ), f"crosswalk_backfill.py imports aphelion: {alias.name!r}"
    # Also check for string references (belt-and-suspenders).
    source_text = src.read_text(encoding="utf-8").lower()
    # Allow "aphelion_doc_id" (the column name) but not import-style references.
    import_patterns = ["import aphelion", "from aphelion"]
    for pattern in import_patterns:
        assert (
            pattern not in source_text
        ), f"crosswalk_backfill.py contains forbidden reference: {pattern!r}"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


def test_backfill_over_fixture_corpus(conn: sqlite3.Connection) -> None:
    """50 fixture rows → all 50 mapped to crosswalk rows."""
    _add_memories(conn, _USER, 25)
    _add_claims(conn, _USER, 25)

    stats = backfill_crosswalk(conn, user_id=_USER)

    assert stats.rows_examined == 50
    assert stats.rows_inserted == 50
    assert stats.rows_skipped_existing == 0
    assert stats.batch_limit_reached is False
    assert stats.source_breakdown["memory"] == 25
    assert stats.source_breakdown["claim"] == 25
    assert _crosswalk_count(conn, _USER) == 50

    # Verify all rows have state='mapped' and non-null content_hash.
    rows = conn.execute(
        "SELECT state, content_hash FROM crosswalk WHERE user_id = ?", (_USER,)
    ).fetchall()
    assert len(rows) == 50
    for row in rows:
        assert row[0] == "mapped"
        assert row[1] is not None and len(row[1]) > 0


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _get_orphan_counter_value(user_id: str) -> float:
    """Read the current counter value from the prometheus default registry.

    prometheus_client Counter's metric.name is the base name without
    the ``_total`` suffix; the sample name carries ``_total``.
    """
    try:
        for metric in prometheus_client.REGISTRY.collect():
            if metric.name == "parallax_crosswalk_miss_orphan":
                for sample in metric.samples:
                    if (
                        sample.name == "parallax_crosswalk_miss_orphan_total"
                        and sample.labels.get("user_id") == user_id
                    ):
                        return sample.value
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Streaming regression: fetchall must NOT be called for memories/claims SELECTs
# ---------------------------------------------------------------------------


class _FetchallTrackingConnection:
    """Thin wrapper around sqlite3.Connection that tracks fetchall calls on
    source SELECT cursors (memories/claims). Used only for streaming regression."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.fetchall_called_for: list[str] = []

    def execute(self, sql: str, params=()):
        cursor = self._inner.execute(sql, params)
        if "FROM memories WHERE user_id" in sql or "FROM claims WHERE user_id" in sql:
            tracker = self
            sql_snippet = sql.strip()[:60]
            orig_fetchall = cursor.fetchall

            class _WrappedCursor:
                def fetchall(self_):  # noqa: N805
                    tracker.fetchall_called_for.append(sql_snippet)
                    return orig_fetchall()

                def __iter__(self_):  # noqa: N805
                    return iter(cursor)

                def __getattr__(self_, name: str):  # noqa: N805
                    return getattr(cursor, name)

            return _WrappedCursor()
        return cursor

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_backfill_streams_rows_no_fetchall(conn: sqlite3.Connection) -> None:
    """Regression: backfill must iterate cursor rows without calling fetchall().

    Inserts 50 memories + 50 claims but sets batch_limit=5.
    The tracking connection wraps the memories/claims cursors and records any
    fetchall() invocation. After backfill_crosswalk returns, asserts no
    fetchall was called, rows_examined==5, batch_limit_reached==True.
    """
    _add_memories(conn, _USER, 50)
    _add_claims(conn, _USER, 50)

    tracking_conn = _FetchallTrackingConnection(conn)
    stats = backfill_crosswalk(tracking_conn, user_id=_USER, batch_limit=5)  # type: ignore[arg-type]

    assert (
        tracking_conn.fetchall_called_for == []
    ), f"fetchall was called on source SELECT(s): {tracking_conn.fetchall_called_for}"
    assert stats.rows_examined == 5
    assert stats.batch_limit_reached is True


# ---------------------------------------------------------------------------
# Backfill must refuse a connection that is wrapped by a live SQLiteGate
# ---------------------------------------------------------------------------


def test_backfill_refuses_gated_connection(tmp_path):
    """Running backfill on a conn that is also serving dual-read traffic
    would race the cross-thread sqlite invariant → SIGSEGV.  The guard
    must raise ValueError with a clear message.
    """
    from parallax.migrations import migrate_to_latest
    from parallax.router.sqlite_gate import SQLiteGate
    from parallax.sqlite_store import connect

    db = tmp_path / "p.db"
    conn = connect(db)
    try:
        migrate_to_latest(conn)
        gate = SQLiteGate(conn, component="m3_dual_read")  # registers in registry
        assert gate is not None  # keep alive — registry holds a weakref

        with pytest.raises(ValueError, match="SQLiteGate"):
            backfill_crosswalk(conn, user_id="u1")
    finally:
        SQLiteGate._active_gate_by_conn_id.pop(id(conn), None)
        conn.close()
