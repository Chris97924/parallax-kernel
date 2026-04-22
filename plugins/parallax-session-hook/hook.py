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

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

_DEFAULT_URL = "http://127.0.0.1:8765"
_DEFAULT_USER = "chris"
_DEFAULT_TIMEOUT = 3.0


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


def main() -> int:
    base_url = _env("PARALLAX_API_URL", _DEFAULT_URL)
    user_id = _env("PARALLAX_USER_ID", _DEFAULT_USER)
    token = os.environ.get("PARALLAX_TOKEN", "").strip()
    try:
        timeout = float(_env("PARALLAX_HOOK_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = _DEFAULT_TIMEOUT

    reminder = _fetch_reminder(base_url, user_id, token, timeout)
    if reminder:
        sys.stdout.write(reminder)
        sys.stdout.write("\n")
    # Always exit 0 — never block the session on a Parallax failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
