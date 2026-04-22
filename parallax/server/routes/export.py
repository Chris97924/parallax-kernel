"""GET /export routes — render memory_cards back to MEMORY.md format.

``GET /export/memory_md`` reads all memory_cards rows for a user, applies the
D4 privacy filter (belt-and-braces on top of ingest-time filtering), then
renders MEMORY.md text plus companion file bodies.

Rendering contract
------------------
* Section order fixed: User → Projects (Active) → Feedback → Reference
* Each section header is always emitted (skeleton is stable even when empty)
* Blank line between sections
* Within a section: rows sorted by (name ASC, filename ASC)
* Card line: ``- [{name}]({filename}) — {description}``  (em-dash U+2014)
* File ends with a single trailing newline
* ``companion_files`` dict: key=filename, value=body; sorted by filename
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from parallax.memory_md import body_looks_like_secret
from parallax.server.auth import current_user_id, require_auth
from parallax.server.deps import get_conn
from parallax.server.schemas import ExportMemoryMdResponse

__all__ = ["router"]

router = APIRouter(
    prefix="/export",
    tags=["export"],
    dependencies=[Depends(require_auth)],
)

_SECTION_ORDER: list[tuple[str, str]] = [
    ("user", "User"),
    ("project", "Projects (Active)"),
    ("feedback", "Feedback"),
    ("reference", "Reference"),
]

_EM_DASH = "—"


def _fetch_cards(
    conn: sqlite3.Connection, *, user_id: str
) -> list[dict[str, Any]]:
    """Return all memory_cards rows for *user_id* as plain dicts."""
    rows = conn.execute(
        "SELECT category, name, filename, description, body "
        "FROM memory_cards WHERE user_id = ? "
        "ORDER BY name ASC, filename ASC",
        (user_id,),
    ).fetchall()
    return [
        {
            "category": r[0],
            "name": r[1],
            "filename": r[2],
            "description": r[3],
            "body": r[4],
        }
        for r in rows
    ]


def _render(cards: list[dict[str, Any]]) -> ExportMemoryMdResponse:
    """Build MEMORY.md text + companion_files from pre-sorted card dicts."""
    buckets: dict[str, list[dict[str, Any]]] = {cat: [] for cat, _ in _SECTION_ORDER}
    for card in cards:
        bucket = buckets.get(card["category"])
        if bucket is not None:
            bucket.append(card)

    parts: list[str] = []
    for cat, heading in _SECTION_ORDER:
        parts.append(f"# {heading}")
        for card in buckets[cat]:
            parts.append(
                f"- [{card['name']}]({card['filename']}) {_EM_DASH} {card['description']}"
            )
        parts.append("")
    memory_md = "\n".join(parts) + "\n"

    companion_files: dict[str, str] = {
        card["filename"]: card["body"]
        for card in sorted(cards, key=lambda c: c["filename"])
    }

    return ExportMemoryMdResponse(
        memory_md=memory_md,
        companion_files=companion_files,
    )


@router.get("/memory_md", response_model=ExportMemoryMdResponse)
def get_export_memory_md(
    request: Request,
    user_id: str | None = Query(None, min_length=1, max_length=128),
    conn: sqlite3.Connection = Depends(get_conn),  # noqa: B008
) -> ExportMemoryMdResponse:
    resolved_user_id = current_user_id(request, user_id)
    all_cards = _fetch_cards(conn, user_id=resolved_user_id)

    # Belt-and-braces filter: drop secret-bearing rows even if they somehow
    # bypassed ingest-time filtering.
    cards = [c for c in all_cards if not body_looks_like_secret(c["body"])]

    return _render(cards)
