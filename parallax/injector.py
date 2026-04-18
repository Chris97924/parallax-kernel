"""SessionStart <system-reminder> injector.

Renders the L1 projection of recent_context + by_file + by_decision hits
into a ``<system-reminder>`` block suitable for piping into Claude Code's
SessionStart hook. Length-capped at 2000 chars — callers should assume a
tight context budget.
"""

from __future__ import annotations

import json
import sqlite3

from parallax.retrieve import FILE_EVENT_TYPES, by_decision, by_file, recent_context
from parallax.sqlite_store import query

__all__ = ["build_session_reminder", "MAX_REMINDER_CHARS"]


MAX_REMINDER_CHARS = 2000
_TRUNCATED_SUFFIX = "... (truncated)"


def _latest_session_id(conn: sqlite3.Connection, user_id: str) -> str | None:
    rows = query(
        conn,
        "SELECT session_id FROM events WHERE user_id = ? AND event_type = 'session.start' "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    return rows[0]["session_id"] if rows else None


def _recent_files(conn: sqlite3.Connection, *, user_id: str, limit: int) -> list[str]:
    """Extract distinct file paths from the latest session's file-edit events."""
    session_id = _latest_session_id(conn, user_id)
    placeholders = ",".join("?" * len(FILE_EVENT_TYPES))
    if session_id is None:
        rows = query(
            conn,
            f"SELECT payload_json FROM events WHERE user_id = ? "
            f"AND event_type IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, *FILE_EVENT_TYPES, limit * 4),
        )
    else:
        rows = query(
            conn,
            f"SELECT payload_json FROM events WHERE user_id = ? AND session_id = ? "
            f"AND event_type IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, session_id, *FILE_EVENT_TYPES, limit * 4),
        )
    paths: list[str] = []
    seen: set[str] = set()
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        tool_input = payload.get("tool_input") or {}
        p = tool_input.get("file_path") or tool_input.get("path") or payload.get("file_path")
        if not p or p in seen:
            continue
        seen.add(p)
        paths.append(str(p))
        if len(paths) >= limit:
            break
    return paths


def _render_section(title: str, entries: list[str]) -> list[str]:
    lines = [f"{title}:"]
    if not entries:
        lines.append("  (none)")
    else:
        for e in entries:
            lines.append(f"  - {e}")
    return lines


def _trim_to_cap(lines: list[str], cap: int) -> str:
    # Defensive copy — this function must not mutate the caller's list.
    local = list(lines)
    out = "\n".join(local)
    if len(out) <= cap:
        return out
    # Drop trailing lines (oldest entries land last in each section rendering)
    # until the block + truncation marker fits the cap.
    marker_budget = len(_TRUNCATED_SUFFIX) + 1  # +1 for the joining newline
    while len(local) > 1 and len(out) + marker_budget > cap:
        local.pop()
        out = "\n".join(local)
    # If even the first line + marker doesn't fit, hard-slice the line
    # itself rather than emitting a mid-marker truncation.
    if len(out) + marker_budget > cap:
        out = out[: max(cap - marker_budget, 0)]
    return f"{out}\n{_TRUNCATED_SUFFIX}"


def build_session_reminder(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    session_id: str | None = None,
    max_hits: int = 8,
) -> str:
    """Build a <system-reminder> string summarizing recent session activity.

    Always includes two sections: 'Recently modified files' and 'Last 3
    decisions'. Empty sections render as '(none)' so downstream diff tools
    can detect empty sessions without crashing. Output capped at
    ``MAX_REMINDER_CHARS`` (2000) with an explicit truncation marker.
    """
    files = _recent_files(conn, user_id=user_id, limit=max(3, max_hits // 2))

    decisions = by_decision(conn, user_id=user_id, limit=3)
    decision_lines = [
        f"{d.project(1)['title']}  (score={d.score:.3f})" for d in decisions
    ]

    # Pull recent_context for additional signal when the caller asks for a
    # specific session — keeps the module self-contained at <= max_hits lines.
    if session_id is not None:
        ctx = recent_context(conn, user_id=user_id, session_id=session_id, limit=max_hits)
        ctx_lines = [f"{c.project(1)['title']}  (score={c.score:.3f})" for c in ctx[:3]]
    else:
        ctx_lines = []

    inner: list[str] = []
    inner.extend(_render_section("Recently modified files", files[: max(3, max_hits // 2)]))
    inner.extend(_render_section("Last 3 decisions", decision_lines))
    if ctx_lines:
        inner.extend(_render_section("Recent context", ctx_lines))

    # Also surface by_file hits when files were found so callers see events,
    # not just paths.
    if files:
        extra: list[str] = []
        for p in files[:3]:
            hits = by_file(conn, user_id=user_id, path=p, limit=1)
            if hits:
                extra.append(f"{p} → {hits[0].project(1)['title']}")
        if extra:
            inner.extend(_render_section("Top file-edit hit", extra))

    wrapper_len = len("<system-reminder>\n</system-reminder>")
    body = _trim_to_cap(inner, MAX_REMINDER_CHARS - wrapper_len - 2)
    return f"<system-reminder>\n{body}\n</system-reminder>"
