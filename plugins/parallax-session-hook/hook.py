#!/usr/bin/env python3
"""SessionStart hook for Claude Code — Parallax reminder fetcher.

Contract (Claude Code hook protocol):

* stdout → injected as additional context for the session
* stderr → surfaces as a visible warning only when the user runs with debug
* exit 0 → success (or silent failure — see below)
* exit non-zero → hook error, may block session start (we avoid this)

Design rule: a degraded Parallax server MUST NOT block Claude sessions.
Every failure path exits 0 with empty stdout unless ``PARALLAX_HOOK_DEBUG``
is truthy, in which case the reason is logged to stderr for operator
diagnosis. This matches the repo-wide "never silently swallow errors"
rule at the boundary — we *do* log, we just don't *fail*.

Dependencies: stdlib only. Uses :mod:`urllib.request` instead of httpx so
the hook runs in any Python 3.11+ environment without the server extras.
"""

import dataclasses
import datetime
import json
import os
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_DEFAULT_URL = "http://127.0.0.1:8765"
_DEFAULT_USER = "chris"
_DEFAULT_TIMEOUT = 3.0
_DEFAULT_WAL_PATH = pathlib.Path.home() / ".parallax_wal.db"

# ---------------------------------------------------------------------------
# Inline WAL — stdlib-only, no parallax package import
# ---------------------------------------------------------------------------

_WAL_DRAIN_BATCH = 100
_DEAD_ROW_TTL_DAYS = 7

_WAL_CREATE_TABLE = """
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
class _DrainResult:
    sent: int
    failed: int
    skipped: int


class _WALQueue:
    """Inlined copy of WALQueue (parallax/wal.py) — stdlib-only. Keep in sync manually."""

    def __init__(self, db_path: pathlib.Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self._db_path.exists()
        conn = sqlite3.connect(str(self._db_path))
        if not existed:
            os.chmod(str(self._db_path), 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute(_WAL_CREATE_TABLE)
        conn.commit()
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        conn.close()

    def __enter__(self) -> "_WALQueue":
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
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM wal_queue").fetchone()
            return int(row[0])
        finally:
            conn.close()

    def drain(self, base_url: str, timeout: float = 3.0) -> _DrainResult:
        """Send queued events; 2xx→delete, 4xx→delete, 5xx/network→retry."""
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            return _DrainResult(sent=0, failed=0, skipped=0)
        conn = self._connect()
        sent = failed = skipped = 0
        try:
            eviction_cutoff = (
                datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=_DEAD_ROW_TTL_DAYS)
            ).isoformat(timespec="microseconds")
            conn.execute(
                "DELETE FROM wal_queue WHERE attempts >= 5 AND created_at < ?",
                (eviction_cutoff,),
            )
            conn.commit()

            rows = conn.execute(
                "SELECT seq, endpoint, payload, user_id, token, attempts"
                " FROM wal_queue ORDER BY seq LIMIT ?",
                (_WAL_DRAIN_BATCH,),
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

        return _DrainResult(sent=sent, failed=failed, skipped=skipped)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _debug() -> bool:
    return os.environ.get("PARALLAX_HOOK_DEBUG", "").strip() not in ("", "0", "false", "False")


def _log_debug(msg: str) -> None:
    if _debug():
        print(f"[parallax-session-hook] {msg}", file=sys.stderr)


def _is_safe_url(base_url: str) -> bool:
    """Reject schemes other than http/https.

    ``PARALLAX_API_URL`` is an env var — if an attacker can seed it
    (CI secret leak, `.env` poisoning, shared dev container) a
    ``file://`` or ``ftp://`` value would make us ship the Bearer token
    to an arbitrary destination. Lock the scheme at the boundary.
    """
    try:
        parsed = urllib.parse.urlparse(base_url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _fetch_reminder(base_url: str, user_id: str, token: str, timeout: float) -> str | None:
    if not _is_safe_url(base_url):
        _log_debug(f"refusing unsafe PARALLAX_API_URL: {base_url!r}")
        return None
    params = urllib.parse.urlencode({"user_id": user_id})
    url = f"{base_url.rstrip('/')}/query/reminder?{params}"
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — localhost
            if resp.status != 200:
                _log_debug(f"server returned HTTP {resp.status}")
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        _log_debug(f"network error: {exc}")
        return None
    except (TimeoutError, OSError) as exc:
        _log_debug(f"timeout/os error: {exc}")
        return None
    except (ValueError, json.JSONDecodeError) as exc:
        _log_debug(f"bad JSON body: {exc}")
        return None

    reminder = payload.get("reminder")
    if not isinstance(reminder, str) or not reminder:
        _log_debug("empty reminder payload")
        return None
    return reminder


def _drain_wal(base_url: str, token: str, timeout: float) -> None:
    """Drain the local WAL before fetching the reminder. Fail-silent."""
    if not _is_safe_url(base_url):
        _log_debug(f"refusing unsafe base_url for WAL drain: {base_url!r}")
        return
    wal_path_str = os.environ.get("PARALLAX_WAL_PATH", "").strip()
    wal_path = pathlib.Path(wal_path_str) if wal_path_str else _DEFAULT_WAL_PATH
    try:
        with _WALQueue(wal_path) as wal:
            if wal.pending_count() == 0:
                return
            result = wal.drain(base_url, timeout=timeout)
            _log_debug(
                f"WAL drain: sent={result.sent} failed={result.failed} skipped={result.skipped}"
            )
    except Exception as exc:  # noqa: BLE001
        _log_debug(f"WAL drain error (ignored): {exc}")


def main() -> int:
    base_url = _env("PARALLAX_API_URL", _DEFAULT_URL)
    user_id = _env("PARALLAX_USER_ID", _DEFAULT_USER)
    token = os.environ.get("PARALLAX_TOKEN", "").strip()
    try:
        timeout = float(_env("PARALLAX_HOOK_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = _DEFAULT_TIMEOUT

    _drain_wal(base_url, token, timeout)

    reminder = _fetch_reminder(base_url, user_id, token, timeout)
    if reminder:
        sys.stdout.write(reminder)
        sys.stdout.write("\n")
    # Always exit 0 — never block the session on a Parallax failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
