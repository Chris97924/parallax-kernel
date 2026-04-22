"""Crosswalk seed mapping: legacy Intent/RetrieveKind strings -> QueryType (Lane D-1).

CROSSWALK_SEED is a read-only MappingProxyType. Intent.FALLBACK is intentionally
absent — resolve() raises UnroutableQueryError when a key is missing.
"""

from __future__ import annotations

import hashlib
import json
import types

from parallax.router.types import QueryType

__all__ = ["CROSSWALK_SEED", "resolve", "UnroutableQueryError", "seed_hash"]

_RAW: dict[str, QueryType] = {
    "Intent.TEMPORAL": QueryType.TEMPORAL_CONTEXT,
    "Intent.MULTI_SESSION": QueryType.RECENT_CONTEXT,
    "Intent.PREFERENCE": QueryType.ENTITY_PROFILE,
    "Intent.USER_FACT": QueryType.ENTITY_PROFILE,
    "Intent.KNOWLEDGE_UPDATE": QueryType.CHANGE_TRACE,
    "RetrieveKind.recent": QueryType.RECENT_CONTEXT,
    "RetrieveKind.file": QueryType.ARTIFACT_CONTEXT,
    "RetrieveKind.decision": QueryType.CHANGE_TRACE,
    "RetrieveKind.bug": QueryType.CHANGE_TRACE,
    "RetrieveKind.entity": QueryType.ENTITY_PROFILE,
    "RetrieveKind.timeline": QueryType.TEMPORAL_CONTEXT,
}

CROSSWALK_SEED: types.MappingProxyType[str, QueryType] = types.MappingProxyType(_RAW)


class UnroutableQueryError(Exception):
    """Raised when a legacy key has no mapping in CROSSWALK_SEED."""


def resolve(legacy: str) -> QueryType:
    """Look up a legacy Intent/RetrieveKind string and return its QueryType.

    Raises UnroutableQueryError when the key is absent (e.g. Intent.FALLBACK).
    Never silently maps to a sentinel.
    """
    try:
        return CROSSWALK_SEED[legacy]
    except KeyError:
        raise UnroutableQueryError(
            f"legacy key {legacy!r} is unmapped (likely Intent.FALLBACK)"
        ) from None


def seed_hash() -> str:
    """Return the sha256 hex digest of the seed mapping (deterministic).

    Serialization: json.dumps with QueryType coerced to .value, sort_keys=True,
    no whitespace (separators=(',', ':')).
    """
    serializable = {k: v.value for k, v in CROSSWALK_SEED.items()}
    payload = json.dumps(serializable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
