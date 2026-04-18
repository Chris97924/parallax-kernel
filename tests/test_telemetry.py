"""Tests for parallax/telemetry.py."""

from __future__ import annotations

import io
import json
import logging
import pathlib
import sqlite3
import tempfile
import threading

import pytest

from parallax import telemetry


@pytest.fixture(autouse=True)
def _fresh_state():
    telemetry.reset()
    yield
    telemetry.reset()


def _attach_buffer(name: str) -> tuple[logging.Logger, io.StringIO]:
    logger = telemetry.get_logger(name)
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logger.handlers[0].formatter)
    logger.addHandler(h)
    return logger, buf


def _lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]


def test_get_logger_is_json_and_idempotent() -> None:
    a = telemetry.get_logger("parallax.telemetry.test")
    b = telemetry.get_logger("parallax.telemetry.test")
    assert a is b
    assert (
        sum(1 for h in a.handlers if getattr(h, "_parallax_telemetry", False))
        == 1
    )


def test_emit_dedup_hit_one_line_info() -> None:
    logger, buf = _attach_buffer("parallax.t.dedup")
    telemetry.emit_dedup_hit(logger, kind="memory", user_id="u1")
    rows = _lines(buf)
    assert len(rows) == 1
    assert rows[0]["event"] == "dedup_hit"
    assert rows[0]["level"] == "INFO"
    assert rows[0]["kind"] == "memory"


def test_emit_state_changed_one_line_info() -> None:
    logger, buf = _attach_buffer("parallax.t.state")
    telemetry.emit_state_changed(logger, before="auto", after="confirmed")
    rows = _lines(buf)
    assert len(rows) == 1
    assert rows[0]["event"] == "state_changed"
    assert rows[0]["level"] == "INFO"
    assert rows[0]["after"] == "confirmed"


def test_emit_orphan_rejected_one_line_info() -> None:
    logger, buf = _attach_buffer("parallax.t.orphan")
    telemetry.emit_orphan_rejected(logger, target_id="m123")
    rows = _lines(buf)
    assert len(rows) == 1
    assert rows[0]["event"] == "orphan_rejected"
    assert rows[0]["level"] == "INFO"


def test_emit_ingest_error_error_level_and_last_error_and_counter() -> None:
    logger, buf = _attach_buffer("parallax.t.err")
    telemetry.emit_ingest_error(logger, kind="claim", user_id="u1", error="boom")
    rows = _lines(buf)
    assert len(rows) == 1
    assert rows[0]["event"] == "ingest_error"
    assert rows[0]["level"] == "ERROR"
    snap = telemetry.snapshot()
    assert snap["errors_total"] == 1
    assert snap["last_error"] is not None
    assert "boom" in snap["last_error"]


def test_inc_and_snapshot_roundtrip() -> None:
    telemetry.inc("ingested_total", 3)
    telemetry.inc("dedup_hits_total")
    telemetry.inc("errors_total", 2)
    snap = telemetry.snapshot()
    assert snap["ingested_total"] == 3
    assert snap["dedup_hits_total"] == 1
    assert snap["errors_total"] == 2


def test_latency_percentiles_on_known_set_empty_returns_zero() -> None:
    snap = telemetry.snapshot()
    assert snap["latency_p50_ms"] == 0.0
    assert snap["latency_p95_ms"] == 0.0
    assert snap["latency_p99_ms"] == 0.0

    for v in range(1, 101):
        telemetry.observe_latency_ms(float(v))
    snap = telemetry.snapshot()
    assert 49 <= snap["latency_p50_ms"] <= 51
    assert 94 <= snap["latency_p95_ms"] <= 96
    assert 98 <= snap["latency_p99_ms"] <= 100


def test_latency_ring_buffer_capped_at_1024() -> None:
    for v in range(5000):
        telemetry.observe_latency_ms(float(v))
    telemetry.snapshot()
    assert len(telemetry._LATENCIES) == 1024


def test_thread_safety_inc_and_observe() -> None:
    def worker() -> None:
        for _ in range(200):
            telemetry.inc("ingested_total")
            telemetry.observe_latency_ms(1.5)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = telemetry.snapshot()
    assert snap["ingested_total"] == 10_000
    assert snap["latency_p99_ms"] >= 0.0


def test_health_on_fresh_bootstrap_db(tmp_path: pathlib.Path) -> None:
    schema = (pathlib.Path(__file__).resolve().parent.parent / "schema.sql").read_text(
        encoding="utf-8"
    )
    db = tmp_path / "parallax.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(schema)
    conn.close()
    h = telemetry.health(db)
    assert h["db_path"] == str(db.resolve())
    assert h["journal_mode"] == "wal"
    assert h["last_error"] is None
    for t in ("sources", "memories", "claims", "decisions", "events", "index_state"):
        assert h["table_counts"][t] == 0


def test_health_missing_tables_surface_as_zero(tmp_path: pathlib.Path) -> None:
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.close()
    h = telemetry.health(db)
    for t in ("sources", "memories", "claims", "decisions", "events", "index_state"):
        assert h["table_counts"][t] == 0


def test_health_reflects_last_error() -> None:
    logger = telemetry.get_logger("parallax.t.health.err")
    telemetry.emit_ingest_error(logger, error="db locked")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        h = telemetry.health(path)
        assert h["last_error"] is not None
        assert "db locked" in h["last_error"]
    finally:
        pathlib.Path(path).unlink(missing_ok=True)


def test_health_exported_from_package_root() -> None:
    import parallax

    assert hasattr(parallax, "health")
    assert parallax.health is telemetry.health
    assert "health" in parallax.__all__


def test_ingest_memory_increments_ingested_total(conn) -> None:
    from parallax.ingest import ingest_memory

    ingest_memory(
        conn, user_id="u1", title="t", summary="s", vault_path="v.md", source_id=None
    )
    assert telemetry.snapshot()["ingested_total"] == 1


def test_ingest_claim_dedup_emits_and_increments(conn) -> None:
    from parallax.ingest import ingest_claim

    ingest_claim(
        conn, user_id="u1", subject="a", predicate="b", object_="c", source_id=None
    )
    ingest_claim(
        conn, user_id="u1", subject="a", predicate="b", object_="c", source_id=None
    )
    snap = telemetry.snapshot()
    assert snap["ingested_total"] == 2
    assert snap["dedup_hits_total"] == 1


def test_ingest_error_path_emits_and_reraises(conn) -> None:
    from parallax.ingest import ingest_claim

    conn.close()
    with pytest.raises(sqlite3.ProgrammingError):
        ingest_claim(
            conn, user_id="u1", subject="a", predicate="b", object_="c", source_id=None
        )
    snap = telemetry.snapshot()
    assert snap["errors_total"] >= 1
    assert snap["last_error"] is not None
