"""Local SQLite Write-Ahead Log for offline queuing of Parallax ingest events.

Stdlib-only: sqlite3, json, pathlib, urllib.request, datetime, dataclasses.
No parallax server package imports — safe to copy into stdlib-only clients.

Delivery semantics: at-least-once. A crash between DELETE and commit causes
the row to be re-sent on the next drain call. Server endpoints should be
idempotent where possible.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import pathlib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

__all__ = ["WALQueue", "DrainResult"]

DRAIN_BATCH = 100
_DEAD_ROW_TTL_DAYS = 7

# Stdlib logging keeps wal.py copyable to stdlib-only clients without
# adding parallax.obs.log as a dependency. The existing hook.py inline
# copy uses _log_debug (env-gated stderr) for the same purpose.
_log = logging.getLogger("parallax.wal")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS wal_queue (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    token      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
)
"""


@dataclasses.dataclass(frozen=True)
class DrainResult:
    sent: int
    failed: int
    skipped: int


class WALQueue:
    """SQLite-backed offline queue for Parallax event writes.

    All public methods are safe to call without the context manager — each
    call self-heals the schema on first use. The context manager exists for
    explicit initialisation and is still the recommended usage pattern.

    Delivery semantics: at-least-once. See module docstring.

    Usage::

        with WALQueue(pathlib.Path("~/.parallax_wal.db").expanduser()) as wal:
            wal.enqueue("/ingest/event", payload, user_id, token)
            result = wal.drain("http://127.0.0.1:8765", timeout=3.0)
    """

    def __init__(self, db_path: pathlib.Path | str) -> None:
        self._db_path = pathlib.Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        """Open a connection, initialise schema, and enforce 0o600 on new files."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self._db_path.exists()
        conn = sqlite3.connect(str(self._db_path))
        if not existed:
            # Restrict to owner-only on creation; tokens are stored in plaintext.
            os.chmod(str(self._db_path), 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute(_CREATE_TABLE)
        conn.commit()
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        conn.close()

    def __enter__(self) -> WALQueue:
        self._ensure_schema()
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def enqueue(
        self,
        endpoint: str,
        payload_dict: dict[str, Any],
        user_id: str,
        token: str,
    ) -> int:
        """Insert a row into wal_queue; return the autoincrement seq."""
        conn = self._connect()
        try:
            created_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds")
            cur = conn.execute(
                "INSERT INTO wal_queue (endpoint, payload, user_id, token, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (endpoint, json.dumps(payload_dict), user_id, token, created_at),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]  # non-None after INSERT
        finally:
            conn.close()

    def pending_count(self) -> int:
        """Return total number of rows in wal_queue."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM wal_queue").fetchone()
            return int(row[0])
        finally:
            conn.close()

    def drain(self, base_url: str, timeout: float = 3.0) -> DrainResult:
        """Send queued events to the Parallax server.

        Rejects non-http/https base_url immediately (returns empty DrainResult).
        Evicts dead rows (attempts >= 5, older than 7 days) before sending.
        Processes at most ``DRAIN_BATCH`` rows per call (ordered by seq).

        - Row with ``attempts >= 5`` (recent): skipped, not deleted.
        - HTTP 2xx: row deleted, ``sent`` incremented.
        - HTTP 4xx: row deleted (permanent client error), ``failed`` incremented.
        - HTTP 5xx / network error: ``attempts`` incremented, ``failed`` incremented.
        """
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            return DrainResult(sent=0, failed=0, skipped=0)

        conn = self._connect()
        sent = failed = skipped = 0
        try:
            eviction_cutoff = (
                datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=_DEAD_ROW_TTL_DAYS)
            ).isoformat(timespec="microseconds")
            cursor = conn.execute(
                "DELETE FROM wal_queue WHERE attempts >= 5 AND created_at < ?",
                (eviction_cutoff,),
            )
            evicted_count = cursor.rowcount
            conn.commit()
            if evicted_count > 0:
                _log.info(
                    "wal_dead_rows_evicted",
                    extra={
                        "event": "wal_dead_rows_evicted",
                        "evicted_count": evicted_count,
                        "ttl_days": _DEAD_ROW_TTL_DAYS,
                    },
                )

            rows = conn.execute(
                "SELECT seq, endpoint, payload, user_id, token, attempts"
                " FROM wal_queue ORDER BY seq LIMIT ?",
                (DRAIN_BATCH,),
            ).fetchall()

            for row in rows:
                seq: int = row["seq"]
                attempts: int = row["attempts"]

                if attempts >= 5:
                    skipped += 1
                    continue

                url = base_url.rstrip("/") + row["endpoint"]
                data = row["payload"].encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(data)),
                        "Authorization": f"Bearer {row['token']}",
                    },
                )

                status: int
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                        status = resp.status
                except urllib.error.HTTPError as exc:
                    status = exc.code
                except (urllib.error.URLError, TimeoutError, OSError):
                    conn.execute(
                        "UPDATE wal_queue SET attempts = attempts + 1 WHERE seq = ?",
                        (seq,),
                    )
                    conn.commit()
                    failed += 1
                    continue

                if 200 <= status < 300:
                    conn.execute("DELETE FROM wal_queue WHERE seq = ?", (seq,))
                    conn.commit()
                    sent += 1
                elif 400 <= status < 500:
                    conn.execute("DELETE FROM wal_queue WHERE seq = ?", (seq,))
                    conn.commit()
                    failed += 1
                else:
                    conn.execute(
                        "UPDATE wal_queue SET attempts = attempts + 1 WHERE seq = ?",
                        (seq,),
                    )
                    conn.commit()
                    failed += 1

        finally:
            conn.close()

        return DrainResult(sent=sent, failed=failed, skipped=skipped)
