"""Tests for parallax.wal — WAL (Write-Ahead Log) queue."""

from __future__ import annotations

import contextlib
import datetime
import sqlite3
import stat
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from parallax.wal import DrainResult, WALQueue

# ---------------------------------------------------------------------------
# Mock HTTP server helpers
# ---------------------------------------------------------------------------


class _BaseHandler(BaseHTTPRequestHandler):
    status: int = 200

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        self.rfile.read(content_len)
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *a: object) -> None:  # silence request logging
        pass


@contextlib.contextmanager
def mock_server(status: int = 200) -> Iterator[str]:
    handler = type("H", (_BaseHandler,), {"status": status})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever)
    t.daemon = True
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_enqueue_pending_count(tmp_path):
    """Enqueue one item; pending_count() should return 1."""
    db_path = tmp_path / "test.db"
    with WALQueue(db_path) as wal:
        wal.enqueue("/ingest/event", {"key": "val"}, "user1", "tok")
        assert wal.pending_count() == 1


def test_schema_auto_created(tmp_path):
    """WALQueue auto-creates wal_queue table; fresh DB has 0 pending."""
    db_path = tmp_path / "fresh.db"
    assert not db_path.exists()
    with WALQueue(db_path) as wal:
        assert wal.pending_count() == 0


def test_enqueue_returns_seq(tmp_path):
    """enqueue() returns an integer autoincrement sequence."""
    db_path = tmp_path / "test.db"
    with WALQueue(db_path) as wal:
        seq = wal.enqueue("/ingest/event", {"key": "val"}, "user1", "tok")
        assert isinstance(seq, int)


def test_drain_success_2xx(tmp_path):
    """Drain against 200 response: pending == 0, sent == 1, failed == failed == 0."""
    db_path = tmp_path / "test.db"
    with WALQueue(db_path) as wal:
        wal.enqueue("/ingest/event", {"key": "val"}, "user1", "tok")
        with mock_server(200) as base_url:
            result = wal.drain(base_url, timeout=2.0)
    assert wal.pending_count() == 0
    assert result.sent == 1
    assert result.failed == 0
    assert result.skipped == 0


def test_drain_4xx_deletes(tmp_path):
    """4xx is a permanent failure — row is deleted, failed count incremented."""
    db_path = tmp_path / "test.db"
    with WALQueue(db_path) as wal:
        wal.enqueue("/ingest/event", {"key": "val"}, "user1", "tok")
        with mock_server(404) as base_url:
            result = wal.drain(base_url, timeout=2.0)
    assert wal.pending_count() == 0
    assert result.failed == 1


def test_drain_5xx_keeps_increments_attempts(tmp_path):
    """5xx is transient — row stays, attempts incremented, failed counted."""
    db_path = tmp_path / "test.db"
    with WALQueue(db_path) as wal:
        wal.enqueue("/ingest/event", {"key": "val"}, "user1", "tok")
        with mock_server(500) as base_url:
            result = wal.drain(base_url, timeout=2.0)
        assert wal.pending_count() == 1
        assert result.failed == 1
        # Verify attempts incremented in DB
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT attempts FROM wal_queue").fetchone()
        conn.close()
        assert row[0] == 1


def test_drain_attempts_gte_5_skips(tmp_path):
    """Row with attempts >= 5 is skipped (not sent, not deleted)."""
    db_path = tmp_path / "test.db"
    with WALQueue(db_path) as wal:
        wal.enqueue("/ingest/event", {"key": "val"}, "user1", "tok")
        # Manually set attempts to 5 via sqlite
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE wal_queue SET attempts = 5")
        conn.commit()
        conn.close()
        with mock_server(200) as base_url:
            result = wal.drain(base_url, timeout=2.0)
        # Row still present
        assert wal.pending_count() == 1
        assert result.skipped == 1
        assert result.sent == 0


def test_drain_batch_limit(tmp_path):
    """Drain processes at most 100 items per call; 150 enqueued → 50 remaining."""
    db_path = tmp_path / "test.db"
    with WALQueue(db_path) as wal:
        for i in range(150):
            wal.enqueue("/ingest/event", {"key": str(i)}, "user1", "tok")
        with mock_server(200) as base_url:
            result = wal.drain(base_url, timeout=5.0)
        assert wal.pending_count() == 50
        assert result.sent == 100


def test_drain_empty_queue(tmp_path):
    """Draining an empty queue returns DrainResult(0, 0, 0)."""
    db_path = tmp_path / "test.db"
    with WALQueue(db_path) as wal:
        with mock_server(200) as base_url:
            result = wal.drain(base_url, timeout=2.0)
    assert result.sent == 0
    assert result.failed == 0
    assert result.skipped == 0


def test_drain_result_is_frozen_dataclass():
    """DrainResult is immutable (frozen dataclass)."""
    result = DrainResult(sent=1, failed=0, skipped=0)
    with pytest.raises((AttributeError, TypeError)):
        result.sent = 99  # type: ignore[misc]


@pytest.mark.skipif(sys.platform == "win32", reason="chmod not meaningful on Windows")
def test_db_permissions(tmp_path):
    """Newly created WAL DB has mode 0o600."""
    db_path = tmp_path / "perms.db"
    with WALQueue(db_path) as wal:
        wal.pending_count()
    mode = stat.S_IMODE(db_path.stat().st_mode)
    assert mode == 0o600


def test_drain_unsafe_url(tmp_path):
    """drain() with a non-http/https scheme returns DrainResult(0,0,0); row untouched."""
    db_path = tmp_path / "unsafe.db"
    with WALQueue(db_path) as wal:
        wal.enqueue("/ingest/event", {"key": "val"}, "user1", "tok")
        result = wal.drain("ftp://evil.example.com")
    assert result == DrainResult(sent=0, failed=0, skipped=0)
    with WALQueue(db_path) as wal:
        assert wal.pending_count() == 1


def test_no_context_manager(tmp_path):
    """pending_count() works without the context manager (schema self-heals)."""
    db_path = tmp_path / "no_cm.db"
    assert WALQueue(db_path).pending_count() == 0


def test_dead_row_eviction(tmp_path):
    """Rows with attempts>=5 and created_at older than 7 days are deleted by drain()."""
    db_path = tmp_path / "evict.db"
    with WALQueue(db_path) as wal:
        wal.enqueue("/ingest/event", {"key": "val"}, "user1", "tok")
        nine_days_ago = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=9)
        ).isoformat(timespec="microseconds")
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE wal_queue SET attempts = 5, created_at = ?", (nine_days_ago,))
        conn.commit()
        conn.close()
        with mock_server(200) as base_url:
            result = wal.drain(base_url, timeout=2.0)
    assert result.skipped == 0
    assert result.sent == 0
    with WALQueue(db_path) as wal:
        assert wal.pending_count() == 0
