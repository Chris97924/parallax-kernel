"""Claude Code hook → events ingestion.

Maps Claude Code session hook payloads onto parallax ``events`` rows. The
expected hook envelope shape is::

    {"hook_event_name": "SessionStart", "session_id": "...", "payload": {...}}

Supported hook_event_name values (v0.3.0 minimum set):

* ``SessionStart``  → event_type ``session.start``
* ``SessionEnd``    → ``session.stop``   (alias: ``Stop``)
* ``UserPromptSubmit`` → ``prompt.submit``
* ``PreToolUse`` (Bash)            → ``tool.bash``
* ``PreToolUse`` (Edit)            → ``tool.edit``
* ``PreToolUse`` (Write)           → ``tool.write``
* ``PostToolUse`` (Edit|Write) on a tracked file → ``file.edit``

File-edit rows may optionally be back-linked to a ``memories`` row when the
file path matches a known ``vault_path``. If the file is not yet ingested as
a memory, we intentionally leave ``target_kind``/``target_id`` as ``None``
— hooks happen at a higher cadence than memory ingestion and orphan
rejection would silently drop half the session stream.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from parallax.events import record_event
from parallax.sqlite_store import query

__all__ = [
    "ingest_hook",
    "ingest_from_json",
    "HOOK_TO_EVENT_TYPE",
]


HOOK_TO_EVENT_TYPE: dict[str, str] = {
    "SessionStart": "session.start",
    "SessionEnd": "session.stop",
    "Stop": "session.stop",
    "UserPromptSubmit": "prompt.submit",
    # PreToolUse / PostToolUse resolved below based on tool name
}


def _file_edit_event_type(hook_type: str, tool_name: str) -> str:
    """Pick event_type for tool-related hooks."""
    tool = (tool_name or "").lower()
    if hook_type == "PostToolUse" and tool in {"edit", "write", "multiedit"}:
        return "file.edit"
    if tool == "bash":
        return "tool.bash"
    if tool in {"edit", "multiedit"}:
        return "tool.edit"
    if tool == "write":
        return "tool.write"
    return f"tool.{tool or 'unknown'}"


def _resolve_target_for_file(
    conn: sqlite3.Connection, *, user_id: str, file_path: str
) -> tuple[str | None, str | None]:
    """Link a file-edit event to a ``memories`` row when possible.

    Returns ``(target_kind, target_id)``; both ``None`` when the file is not
    tracked. Uses a suffix match on ``vault_path`` to tolerate absolute vs
    repo-relative paths.
    """
    if not file_path:
        return (None, None)
    # Escape LIKE wildcards so paths containing '%' or '_' (e.g. 'utils_v2.py')
    # match literally rather than treating those characters as wildcards.
    escaped = (
        file_path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    rows = query(
        conn,
        "SELECT memory_id, vault_path FROM memories WHERE user_id = ? "
        "AND (vault_path = ? OR vault_path LIKE ? ESCAPE '\\') LIMIT 1",
        (user_id, file_path, f"%{escaped}"),
    )
    if not rows:
        return (None, None)
    return ("memory", rows[0]["memory_id"])


def _hash_path(path: str) -> str:
    """Deterministic short id for file-path references that lack a memory row."""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def _require(obj: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise ValueError(f"{where}: missing required field {key!r}")
    return obj[key]


def ingest_hook(
    conn: sqlite3.Connection,
    *,
    hook_type: str,
    session_id: str,
    payload: Mapping[str, Any],
    user_id: str,
    actor: str = "claude-code",
) -> str:
    """Map a single Claude Code hook fire onto an events row.

    Returns the generated ``event_id``. Raises :class:`ValueError` on
    malformed input (unknown hook_type with no tool_name, missing session_id,
    non-dict payload).
    """
    if not hook_type:
        raise ValueError("ingest_hook: missing required field 'hook_type'")
    if not session_id:
        raise ValueError("ingest_hook: missing required field 'session_id'")
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"ingest_hook: payload must be a mapping, got {type(payload).__name__}"
        )

    target_kind: str | None = None
    target_id: str | None = None

    if hook_type in HOOK_TO_EVENT_TYPE:
        event_type = HOOK_TO_EVENT_TYPE[hook_type]
    elif hook_type in {"PreToolUse", "PostToolUse"}:
        tool_name = str(payload.get("tool_name", "")).strip()
        if not tool_name:
            raise ValueError(
                f"ingest_hook: {hook_type} payload missing required field 'tool_name'"
            )
        event_type = _file_edit_event_type(hook_type, tool_name)
        # PostToolUse on a file-editing tool: try to back-link to memory
        if event_type == "file.edit":
            tool_input = payload.get("tool_input") or {}
            file_path = (
                tool_input.get("file_path")
                or tool_input.get("path")
                or payload.get("file_path")
                or ""
            )
            target_kind, target_id = _resolve_target_for_file(
                conn, user_id=user_id, file_path=str(file_path)
            )
            # embed a stable path fingerprint into payload for retrieval
            if file_path and target_id is None:
                payload = {**dict(payload), "_path_sha16": _hash_path(str(file_path))}
    else:
        raise ValueError(f"ingest_hook: unknown hook_type {hook_type!r}")

    return record_event(
        conn,
        user_id=user_id,
        actor=actor,
        event_type=event_type,
        target_kind=target_kind,
        target_id=target_id,
        payload=dict(payload),
        session_id=session_id,
    )


def ingest_from_json(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    raw_json: str,
    actor: str = "claude-code",
) -> str:
    """Parse a hook envelope JSON and dispatch to :func:`ingest_hook`."""
    try:
        env = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ingest_from_json: invalid JSON ({exc})") from exc
    if not isinstance(env, Mapping):
        raise ValueError("ingest_from_json: envelope must be a JSON object")
    hook_type = _require(env, "hook_event_name", "ingest_from_json")
    session_id = _require(env, "session_id", "ingest_from_json")
    payload = env.get("payload", {})
    return ingest_hook(
        conn,
        hook_type=str(hook_type),
        session_id=str(session_id),
        payload=payload if isinstance(payload, Mapping) else {"raw": payload},
        user_id=user_id,
        actor=actor,
    )
