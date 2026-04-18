"""Event-write helpers for Parallax.

Sits on top of :func:`parallax.sqlite_store.insert_event` and adds:

* ULID generation for ``event_id``
* JSON serialization for ``payload``
* Reference-integrity validation via :func:`parallax.validators.target_ref_exists`
  so events with a non-empty ``(target_kind, target_id)`` cannot reference a
  row that does not exist (orphan rejection)
* Two convenience helpers — :func:`record_memory_reaffirmed` and
  :func:`record_claim_state_changed` — for the two event types that the
  ingest / state-machine layers emit today.

Events without a target (system-level audit rows) are accepted by passing
``target_kind=None`` and ``target_id=None``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from ulid import ULID

from parallax import telemetry
from parallax.obs.log import get_logger
from parallax.sqlite_store import Event, insert_event, now_iso
from parallax.validators import VALID_TARGET_KINDS, target_ref_exists

__all__ = [
    "record_event",
    "record_memory_reaffirmed",
    "record_claim_state_changed",
]

_log = get_logger("parallax.events")
_tlog = telemetry.get_logger("parallax.events.telemetry")


def _ulid() -> str:
    return str(ULID())


def record_event(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    actor: str,
    event_type: str,
    target_kind: str | None,
    target_id: str | None,
    payload: Mapping[str, Any] | None = None,
    approval_tier: str | None = None,
) -> str:
    """Append a single event row. Returns the generated event_id.

    When ``target_kind`` is one of :data:`VALID_TARGET_KINDS` and
    ``target_id`` is a non-empty string, the row referenced by
    ``(target_kind, target_id)`` MUST exist or :class:`ValueError` is raised
    (orphan rejection). System-level events with no target pass
    ``target_kind=None`` and ``target_id=None``.

    The validator and the insert run on the same connection; callers MUST
    hold the surrounding transaction so the existence check and the insert
    observe a consistent snapshot under WAL-mode SQLite (see
    :func:`parallax.validators.target_ref_exists` docstring for the TOCTOU
    rationale).
    """
    if (target_kind is None) != (target_id is None):
        raise ValueError(
            "target_kind and target_id must be provided together or both omitted; "
            f"got target_kind={target_kind!r}, target_id={target_id!r}"
        )

    if target_kind is not None and target_id is not None and target_id != "":
        if target_kind in VALID_TARGET_KINDS:
            if not target_ref_exists(conn, target_kind, target_id):
                telemetry.emit_orphan_rejected(
                    _tlog,
                    user_id=user_id,
                    target_kind=target_kind,
                    target_id=target_id,
                )
                raise ValueError(
                    f"orphan event rejected: ({target_kind!r}, {target_id!r}) "
                    f"does not exist"
                )

    event_id = _ulid()
    payload_json = json.dumps(dict(payload) if payload is not None else {}, sort_keys=True)
    insert_event(
        conn,
        Event(
            event_id=event_id,
            user_id=user_id,
            actor=actor,
            event_type=event_type,
            target_kind=target_kind,
            target_id=target_id,
            payload_json=payload_json,
            approval_tier=approval_tier,
            created_at=now_iso(),
        ),
    )
    _log.info(
        "record_event",
        extra={
            "event": "record_event",
            "event_type": event_type,
            "user_id": user_id,
            "event_id": event_id,
            "target_kind": target_kind,
            "target_id": target_id,
        },
    )
    return event_id


def record_memory_reaffirmed(
    conn: sqlite3.Connection, *, user_id: str, memory_id: str, actor: str = "system"
) -> str:
    """Emit a ``memory.reaffirmed`` event for a deduped ingest hit."""
    return record_event(
        conn,
        user_id=user_id,
        actor=actor,
        event_type="memory.reaffirmed",
        target_kind="memory",
        target_id=memory_id,
        payload={"memory_id": memory_id},
    )


def record_claim_state_changed(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    claim_id: str,
    from_state: str,
    to_state: str,
    actor: str = "system",
) -> str:
    """Emit a ``claim.state_changed`` event capturing the from/to transition."""
    return record_event(
        conn,
        user_id=user_id,
        actor=actor,
        event_type="claim.state_changed",
        target_kind="claim",
        target_id=claim_id,
        payload={"from": from_state, "to": to_state},
    )
