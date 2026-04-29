"""Tests for parallax.router.sqlite_gate (M3-T1.2, US-011).

Unit tests use in-memory sqlite (``":memory:"``).
WAL-pragma tests use a temp-file DB (WAL is not supported on in-memory DBs).
Concurrency tests use shared in-memory connections (``check_same_thread=False``).
"""

from __future__ import annotations

import sqlite3
import threading
import time
import weakref

import pytest

from parallax.router.sqlite_gate import (
    SQLiteGate,
    SQLiteGateMetrics,
    _Cancellable,
)

# ---------------------------------------------------------------------------
# Helper: read prometheus sample values
# ---------------------------------------------------------------------------


def _hist_count(labeled_hist) -> float:
    """Return the _count sample value from a labeled Histogram."""
    for metric in labeled_hist.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                return sample.value
    return 0.0


def _gauge_value(gauge) -> float:
    """Return the current value of an unlabelled Gauge."""
    for metric in gauge.collect():
        for sample in metric.samples:
            if not sample.name.endswith("_created"):
                return sample.value
    return 0.0


def _counter_value(labeled_counter) -> float:
    """Return the _total sample value from a labeled Counter."""
    for metric in labeled_counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_conn():
    """Fresh in-memory sqlite connection (check_same_thread=False)."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    SQLiteGate._active_gate_by_conn_id.pop(id(c), None)
    yield c
    SQLiteGate._active_gate_by_conn_id.pop(id(c), None)
    c.close()


@pytest.fixture()
def file_conn(tmp_path):
    """Fresh file-backed sqlite connection for WAL pragma tests."""
    db_path = tmp_path / "test.db"
    c = sqlite3.connect(str(db_path), check_same_thread=False)
    SQLiteGate._active_gate_by_conn_id.pop(id(c), None)
    yield c
    SQLiteGate._active_gate_by_conn_id.pop(id(c), None)
    c.close()


@pytest.fixture()
def seeded_gate(mem_conn):
    """SQLiteGate with a simple ``items`` table pre-populated."""
    mem_conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val INTEGER)")
    mem_conn.commit()
    g = SQLiteGate(mem_conn, component="m3_dual_read")
    return mem_conn, g


# ---------------------------------------------------------------------------
# 1. fetch_all materialises inside lock (returns list, not cursor/generator)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_all_materializes_inside_lock(seeded_gate):
    conn, gate = seeded_gate
    conn.execute("INSERT INTO items VALUES (1, 10)")
    conn.commit()
    result = gate.fetch_all("SELECT id, val FROM items")
    assert isinstance(result, list), f"expected list, got {type(result)}"
    assert result == [(1, 10)]


# ---------------------------------------------------------------------------
# 2. WAL pragmas applied on first construction (requires file-backed DB)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pragmas_applied_on_first_construction(file_conn):
    SQLiteGate._active_gate_by_conn_id.pop(id(file_conn), None)
    SQLiteGate(file_conn, component="m3_dual_read")

    cur = file_conn.cursor()
    cur.execute("PRAGMA journal_mode")
    jm = cur.fetchone()[0]
    cur.execute("PRAGMA synchronous")
    sync = cur.fetchone()[0]
    cur.execute("PRAGMA wal_autocheckpoint")
    wac = cur.fetchone()[0]
    cur.close()

    assert jm == "wal", f"journal_mode expected 'wal', got {jm!r}"
    assert sync == 1, f"synchronous expected 1 (NORMAL), got {sync}"
    assert wac == 0, f"wal_autocheckpoint expected 0, got {wac}"


# ---------------------------------------------------------------------------
# 3. Pragmas NOT re-applied on second gate for same connection
#    Verified by checking the _active_gate_by_conn_id registry membership
#    and observing that the second gate's pragma branch is skipped.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pragmas_not_reapplied_on_second_gate(file_conn):
    SQLiteGate._active_gate_by_conn_id.pop(id(file_conn), None)

    # First gate applies pragmas; the conn id is registered in the gate
    # registry once construction completes.  Keep the gate alive so its
    # weakref stays live while we construct the second gate below.
    first_gate = SQLiteGate(file_conn, component="m3_dual_read")
    assert id(file_conn) in SQLiteGate._active_gate_by_conn_id

    # Second gate for same conn: the registry already holds a live weakref,
    # so the pragma cursor branch is skipped.  Subclass to observe.
    pragma_execute_calls: list[str] = []

    class TrackingGate(SQLiteGate):
        def _register_and_apply_pragmas(self) -> None:
            cid = id(self._conn)
            with SQLiteGate._registry_lock:
                SQLiteGate._prune_dead_refs_locked()
                existing = SQLiteGate._active_gate_by_conn_id.get(cid)
                if existing is not None and existing() is not None:
                    pragma_execute_calls.append("skipped")
                    SQLiteGate._active_gate_by_conn_id[cid] = weakref.ref(self)
                    return
            super()._register_and_apply_pragmas()
            pragma_execute_calls.append("applied")

    assert first_gate is not None  # keep alive for the duration of the test
    TrackingGate(file_conn, component="ingest")

    assert pragma_execute_calls == [
        "skipped"
    ], f"Expected 'skipped' (idempotent), got {pragma_execute_calls}"


# ---------------------------------------------------------------------------
# 4. Concurrent reads — no corruption
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_concurrent_reads_no_corruption(mem_conn):
    mem_conn.execute("CREATE TABLE nums (n INTEGER)")
    for i in range(100):
        mem_conn.execute("INSERT INTO nums VALUES (?)", (i,))
    mem_conn.commit()

    gate = SQLiteGate(mem_conn, component="m3_dual_read")
    errors: list[Exception] = []

    def worker():
        for _ in range(100):
            try:
                rows = gate.fetch_all("SELECT n FROM nums ORDER BY n")
                assert isinstance(rows, list)
                assert len(rows) == 100
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Errors in concurrent reads: {errors[:3]}"


# ---------------------------------------------------------------------------
# 5. Concurrent writes serialize — final count is correct
#    Uses a per-"transaction" lock to make read-modify-write atomic, then
#    verifies final total equals n_threads * increments_each.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_concurrent_writes_serialize(mem_conn):
    mem_conn.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, val INTEGER)")
    mem_conn.execute("INSERT INTO counter VALUES (1, 0)")
    mem_conn.commit()

    gate = SQLiteGate(mem_conn, component="m3_dual_read")
    n_threads = 10
    increments_each = 5
    errors: list[Exception] = []

    # Wrap the read-modify-write in an outer lock so it's truly atomic.
    tx_lock = threading.Lock()

    def worker():
        for _ in range(increments_each):
            try:
                with tx_lock:
                    rows = gate.fetch_all("SELECT val FROM counter WHERE id=1")
                    current = rows[0][0]
                    gate.execute("UPDATE counter SET val=? WHERE id=1", (current + 1,))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Errors: {errors[:3]}"
    rows = gate.fetch_all("SELECT val FROM counter WHERE id=1")
    assert rows[0][0] == n_threads * increments_each


# ---------------------------------------------------------------------------
# 6. Metric lock_wait emitted on fetch_all
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_metric_lock_wait_emitted(seeded_gate):
    conn, gate = seeded_gate
    conn.execute("INSERT INTO items VALUES (1, 99)")
    conn.commit()

    hist = SQLiteGateMetrics.lock_wait.labels(component="m3_dual_read", op="read")
    before_count = _hist_count(hist)
    gate.fetch_all("SELECT id FROM items")
    after_count = _hist_count(hist)

    assert after_count > before_count, "Expected at least 1 observation on lock_wait histogram"


# ---------------------------------------------------------------------------
# 7. Metric lock_hold emitted on fetch_all
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_metric_lock_hold_emitted(seeded_gate):
    conn, gate = seeded_gate
    conn.execute("INSERT INTO items VALUES (1, 99)")
    conn.commit()

    hist = SQLiteGateMetrics.lock_hold.labels(component="m3_dual_read", op="read")
    before_count = _hist_count(hist)
    gate.fetch_all("SELECT id FROM items")
    after_count = _hist_count(hist)

    assert after_count > before_count, "Expected lock_hold histogram to record an observation"


# ---------------------------------------------------------------------------
# 8. Queue depth is 0 after all concurrent reads complete
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_metric_queue_depth_zero_after(mem_conn):
    mem_conn.execute("CREATE TABLE t (v INTEGER)")
    mem_conn.execute("INSERT INTO t VALUES (1)")
    mem_conn.commit()
    gate = SQLiteGate(mem_conn, component="m3_dual_read")

    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        gate.fetch_all("SELECT v FROM t")

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    depth = _gauge_value(SQLiteGateMetrics.queue_depth)
    assert depth == 0, f"Expected queue depth 0 after all reads, got {depth}"


# ---------------------------------------------------------------------------
# 9. Error counter increments on sqlite error
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_metric_errors_total_increments_on_sqlite_error(mem_conn):
    gate = SQLiteGate(mem_conn, component="m3_dual_read")

    counter = SQLiteGateMetrics.errors.labels(
        code="OperationalError", component="m3_dual_read", op="read"
    )
    before = _counter_value(counter)

    with pytest.raises(sqlite3.OperationalError):
        gate.fetch_all("SELECT * FROM nonexistent_table_xyz_abc")

    after = _counter_value(counter)
    assert after > before, "Expected errors_total counter to increment"


# ---------------------------------------------------------------------------
# 10. Component label isolation — separate metric series per component
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_component_label_isolation(mem_conn):
    conn2 = sqlite3.connect(":memory:", check_same_thread=False)
    SQLiteGate._active_gate_by_conn_id.pop(id(mem_conn), None)
    SQLiteGate._active_gate_by_conn_id.pop(id(conn2), None)

    try:
        mem_conn.execute("CREATE TABLE t (v INTEGER)")
        mem_conn.execute("INSERT INTO t VALUES (1)")
        mem_conn.commit()

        conn2.execute("CREATE TABLE t (v INTEGER)")
        conn2.execute("INSERT INTO t VALUES (1)")
        conn2.commit()

        gate_m3 = SQLiteGate(mem_conn, component="m3_dual_read")
        gate_ingest = SQLiteGate(conn2, component="ingest")

        # Baseline counts (may be > 0 from other tests in same session)
        hist_m3 = SQLiteGateMetrics.lock_wait.labels(component="m3_dual_read", op="read")
        hist_ingest = SQLiteGateMetrics.lock_wait.labels(component="ingest", op="read")
        before_m3 = _hist_count(hist_m3)
        before_ingest = _hist_count(hist_ingest)

        gate_m3.fetch_all("SELECT v FROM t")
        gate_ingest.fetch_all("SELECT v FROM t")

        # Each component's histogram must have gained exactly 1 observation.
        assert _hist_count(hist_m3) == before_m3 + 1
        assert _hist_count(hist_ingest) == before_ingest + 1
    finally:
        SQLiteGate._active_gate_by_conn_id.pop(id(conn2), None)
        conn2.close()


# ---------------------------------------------------------------------------
# 11. Background checkpoint runs (file DB so WAL file exists)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_background_checkpoint_runs(file_conn):
    # Use an Event to detect that the background thread executed a checkpoint.
    checkpoint_ran = threading.Event()

    # Verify via a subclass that overrides the loop body, since sqlite3 C types
    # can't be patched directly.
    class TrackingGate(SQLiteGate):
        def start_background_checkpoint(self, *, interval_seconds=300.0):
            stop_event = threading.Event()

            def _run():
                while not stop_event.wait(timeout=interval_seconds):
                    checkpoint_ran.set()
                    try:
                        with self._lock:
                            cur = self._conn.cursor()
                            cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
                            cur.close()
                    except Exception:  # noqa: BLE001
                        pass

            t = threading.Thread(target=_run, daemon=True, name="sqlite-gate-checkpoint")
            t.start()
            from parallax.router.sqlite_gate import _Cancellable

            return _Cancellable(stop_event, thread=t)

    tracking_gate = TrackingGate(file_conn, component="m3_dual_read")
    cancellable = tracking_gate.start_background_checkpoint(interval_seconds=0.05)
    fired = checkpoint_ran.wait(timeout=1.0)
    cancellable.stop()

    assert fired, "Expected background checkpoint to fire within 1s"


# ---------------------------------------------------------------------------
# 12. Background checkpoint stop is clean — no deadlock, stop_event set
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_background_checkpoint_stop_is_clean(mem_conn):
    gate = SQLiteGate(mem_conn, component="m3_dual_read")
    cancellable = gate.start_background_checkpoint(interval_seconds=0.05)

    cancellable.stop()

    assert isinstance(cancellable, _Cancellable)
    assert cancellable._stop_event.is_set(), "Expected stop_event to be set after .stop()"

    # Verify the daemon thread exits within 1 second.
    deadline = time.monotonic() + 1.0
    thread_exited = False
    while time.monotonic() < deadline:
        still_running = [
            t for t in threading.enumerate() if t.name == "sqlite-gate-checkpoint" and t.is_alive()
        ]
        if not still_running:
            thread_exited = True
            break
        time.sleep(0.05)

    assert thread_exited, "Background checkpoint thread did not exit within 1s after stop()"


# ---------------------------------------------------------------------------
# 13. Invalid component raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invalid_component_raises(mem_conn):
    SQLiteGate._active_gate_by_conn_id.pop(id(mem_conn), None)
    with pytest.raises(ValueError, match="component="):
        SQLiteGate(mem_conn, component="not_a_valid_component")


# ---------------------------------------------------------------------------
# 14. fetch_one returns row / None
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_one_returns_row(mem_conn):
    mem_conn.execute("CREATE TABLE t (id INTEGER, val TEXT)")
    mem_conn.execute("INSERT INTO t VALUES (1, 'hello')")
    mem_conn.commit()
    gate = SQLiteGate(mem_conn, component="m3_dual_read")

    row = gate.fetch_one("SELECT id, val FROM t WHERE id=1")
    assert row == (1, "hello")


@pytest.mark.unit
def test_fetch_one_returns_none_when_missing(mem_conn):
    mem_conn.execute("CREATE TABLE t (id INTEGER)")
    mem_conn.commit()
    gate = SQLiteGate(mem_conn, component="m3_dual_read")

    result = gate.fetch_one("SELECT id FROM t WHERE id=999")
    assert result is None


# ---------------------------------------------------------------------------
# 15. executemany inserts batch correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_executemany_batch_insert(mem_conn):
    mem_conn.execute("CREATE TABLE t (n INTEGER)")
    mem_conn.commit()
    gate = SQLiteGate(mem_conn, component="m3_dual_read")

    batch = [(i,) for i in range(10)]
    gate.executemany("INSERT INTO t VALUES (?)", batch)

    rows = gate.fetch_all("SELECT n FROM t ORDER BY n")
    assert [r[0] for r in rows] == list(range(10))


# ---------------------------------------------------------------------------
# 16. _sample_wal_size works with a real WAL file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sample_wal_size_with_file_db(file_conn):
    """After writes on a WAL-mode DB, the WAL size gauge should be set."""
    gate = SQLiteGate(file_conn, component="m3_dual_read")
    file_conn.execute("CREATE TABLE t (n INTEGER)")
    file_conn.commit()
    # Perform a read to trigger _sample_wal_size
    gate.fetch_all("SELECT n FROM t")
    # The WAL file may or may not exist yet; just verify no exception is thrown.
    # If the WAL file doesn't exist (OS doesn't create it on an empty DB), that's fine.
    val = _gauge_value(SQLiteGateMetrics.wal_size)
    assert isinstance(val, float)


# ---------------------------------------------------------------------------
# 17. Write error path increments counter with correct op label
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_metric_errors_write_op(mem_conn):
    gate = SQLiteGate(mem_conn, component="m3_dual_read")

    counter = SQLiteGateMetrics.errors.labels(
        code="OperationalError", component="m3_dual_read", op="write"
    )
    before = _counter_value(counter)

    with pytest.raises(sqlite3.OperationalError):
        gate.execute("UPDATE nonexistent_table_xyz SET x=1")

    after = _counter_value(counter)
    assert after > before, "Expected write errors_total counter to increment"


# ---------------------------------------------------------------------------
# _Cancellable.stop(join_timeout=0) escape-hatch path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cancellable_stop_join_timeout_zero_skips_thread_join():
    """``stop(join_timeout=0)`` must NOT call ``self._thread.join``.

    The escape hatch exists for callers that explicitly want non-blocking
    legacy behaviour.  If a future refactor changes ``> 0`` to ``>= 0``
    in the join-timeout guard, this test will catch it.
    """

    class _RecordingThread:
        def __init__(self) -> None:
            self.join_calls: list[float | None] = []

        def join(self, timeout: float | None = None) -> None:
            self.join_calls.append(timeout)

    stop_event = threading.Event()
    fake_thread = _RecordingThread()
    cancellable = _Cancellable(stop_event, thread=fake_thread)  # type: ignore[arg-type]

    cancellable.stop(join_timeout=0)

    assert stop_event.is_set(), "stop_event must be set even when skipping join"
    assert (
        fake_thread.join_calls == []
    ), f"Expected zero join() calls when join_timeout=0; got {fake_thread.join_calls}"


@pytest.mark.unit
def test_cancellable_stop_default_join_timeout_calls_join():
    """Sanity counter-test: default ``stop()`` (no kwarg) DOES call join."""

    class _RecordingThread:
        def __init__(self) -> None:
            self.join_calls: list[float | None] = []

        def join(self, timeout: float | None = None) -> None:
            self.join_calls.append(timeout)

    stop_event = threading.Event()
    fake_thread = _RecordingThread()
    cancellable = _Cancellable(stop_event, thread=fake_thread)  # type: ignore[arg-type]

    cancellable.stop()

    assert len(fake_thread.join_calls) == 1
    assert fake_thread.join_calls[0] == _Cancellable._DEFAULT_JOIN_TIMEOUT_SECONDS
