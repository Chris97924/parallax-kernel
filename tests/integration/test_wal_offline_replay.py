"""Integration test for M2 Track 0 #3 DoD: offline queue + reconnect → zero loss.

Spec: 5 events enqueued while server unreachable; on reconnect every event
must drain (sent=5, failed=0, pending=0). The DoD wall-clock window is
10 minutes in production but the property under test ("offline events stay
queued and replay 100% on reconnect") is time-independent — we use a closed
port for the offline phase and a real local server for the reconnect phase.
"""

from __future__ import annotations

import contextlib
import http.server
import threading
from collections.abc import Iterator

import pytest

from parallax.wal import WALQueue

# Privileged-port literal: nothing is ever listening on 127.0.0.1:1, so every
# connection attempt yields ConnectionRefused → urllib.error.URLError, which is
# WALQueue.drain's network-error path (attempts incremented, row retained).
_UNREACHABLE_URL = "http://127.0.0.1:1"


class _Status200Handler(http.server.BaseHTTPRequestHandler):
    """Minimal POST sink that ACKs every request with 200."""

    def do_POST(self) -> None:  # noqa: N802 — http.server contract
        content_len = int(self.headers.get("Content-Length", 0))
        self.rfile.read(content_len)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_: object) -> None:  # silence per-request logging
        pass


@contextlib.contextmanager
def _local_200_server() -> Iterator[str]:
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Status200Handler)
    thread = threading.Thread(target=srv.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


@pytest.mark.parametrize("message_count", [5])
def test_offline_then_reconnect_zero_loss(tmp_path, message_count: int) -> None:
    """5 enqueues during outage + reconnect drain → sent=5, failed=0, pending=0."""
    db_path = tmp_path / "offline_replay.db"

    # Phase 1 — server unreachable. Enqueue 5 events; drain must keep all 5.
    with WALQueue(db_path) as wal:
        for seq in range(message_count):
            wal.enqueue(
                "/ingest/event",
                {"seq": seq, "kind": "memory", "body": f"msg-{seq}"},
                "user1",
                "tok",
            )
        assert wal.pending_count() == message_count

        offline_result = wal.drain(_UNREACHABLE_URL, timeout=0.2)
        assert offline_result.sent == 0, "no event should have left during outage"
        assert (
            offline_result.failed == message_count
        ), "all events should be marked failed-but-retained"
        assert offline_result.skipped == 0
        assert wal.pending_count() == message_count, "all 5 events must remain queued during outage"

    # Phase 2 — reconnect. A real local 200-OK server replaces the closed port.
    # Every queued row must drain on the next call.
    with _local_200_server() as base_url, WALQueue(db_path) as wal:
        reconnect_result = wal.drain(base_url, timeout=2.0)
        assert (
            reconnect_result.sent == message_count
        ), "every offline event must be sent on reconnect"
        assert reconnect_result.failed == 0
        assert reconnect_result.skipped == 0
        assert wal.pending_count() == 0, "queue must be empty after successful reconnect drain"


def test_offline_replay_is_idempotent_on_repeated_drain(tmp_path) -> None:
    """A second drain immediately after a successful one is a no-op (queue empty)."""
    db_path = tmp_path / "idempotent.db"

    with WALQueue(db_path) as wal:
        wal.enqueue("/ingest/event", {"seq": 0}, "user1", "tok")

    with _local_200_server() as base_url, WALQueue(db_path) as wal:
        first = wal.drain(base_url, timeout=2.0)
        assert first.sent == 1
        second = wal.drain(base_url, timeout=2.0)
        assert second == first.__class__(sent=0, failed=0, skipped=0)
        assert wal.pending_count() == 0
