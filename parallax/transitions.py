"""State-transition matrix for Parallax entities.

Codifies the allowed ``(from_state -> to_state)`` graph for each canonical
entity (memories, claims, sources, decisions). Mirrors
``docs/state-transitions.md``; this module is the machine-readable form so
the future state-mutation writers can validate transitions without parsing
markdown.

Self-loops on non-terminal states are allowed (the doc records them as
``Y`` to permit idempotent re-stages). Terminal states map to
``frozenset()`` (no outgoing edges, including no self-loop).
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "MEMORY_TRANSITIONS",
    "CLAIM_TRANSITIONS",
    "SOURCE_TRANSITIONS",
    "DECISION_TRANSITIONS",
    "is_allowed_transition",
]


MEMORY_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "draft": frozenset({"draft", "active", "archived"}),
    "active": frozenset({"active", "archived"}),
    "archived": frozenset(),
}

CLAIM_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "auto": frozenset({"auto", "pending", "confirmed", "rejected"}),
    "pending": frozenset({"pending", "confirmed", "rejected"}),
    "confirmed": frozenset({"confirmed", "rejected"}),
    "rejected": frozenset(),
}

SOURCE_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "ingested": frozenset({"ingested", "parsed", "archived"}),
    "parsed": frozenset({"parsed", "archived"}),
    "archived": frozenset(),
}

DECISION_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "proposed": frozenset({"proposed", "approved", "revoked"}),
    "approved": frozenset({"approved", "applied", "revoked"}),
    "applied": frozenset({"applied", "revoked"}),
    "revoked": frozenset(),
}

_MATRIX: Final[dict[str, dict[str, frozenset[str]]]] = {
    "memory": MEMORY_TRANSITIONS,
    "claim": CLAIM_TRANSITIONS,
    "source": SOURCE_TRANSITIONS,
    "decision": DECISION_TRANSITIONS,
}


def is_allowed_transition(entity: str, from_state: str, to_state: str) -> bool:
    """Return True iff ``entity`` may move from ``from_state`` to ``to_state``.

    Raises :class:`ValueError` when ``entity`` is unknown or ``from_state``
    is not a registered state for that entity. ``to_state`` is not range-
    checked — an unknown ``to_state`` simply returns False.
    """
    try:
        matrix = _MATRIX[entity]
    except KeyError as exc:
        raise ValueError(
            f"unknown entity {entity!r}; expected one of {sorted(_MATRIX)}"
        ) from exc
    if from_state not in matrix:
        raise ValueError(
            f"unknown {entity} from_state {from_state!r}; "
            f"expected one of {sorted(matrix)}"
        )
    return to_state in matrix[from_state]
