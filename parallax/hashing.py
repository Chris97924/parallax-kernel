"""Canonical normalization + sha256 hashing for Parallax content_hash columns.

Schema contract (E:/Parallax/schema.sql):
    memories.content_hash = sha256(normalize(title || summary || vault_path))
    claims.content_hash   = sha256(normalize(subject || predicate || object || source_id))

Both call sites funnel through this single module so dedup semantics stay
identical across writers, migrations, and verifiers.
"""

from __future__ import annotations

import hashlib
import unicodedata

__all__ = ["normalize", "content_hash"]

_SEPARATOR = "||"


def normalize(*parts: str) -> str:
    """Return the canonical string form of ``parts`` for hashing.

    Each part is NFC-normalized, stripped of surrounding whitespace, and
    joined with ``"||"`` to match the schema comments. Internal whitespace is
    preserved.

    ``None`` is rejected with :class:`TypeError` — callers holding
    ``Optional[str]`` values must convert to ``""`` themselves. This keeps
    the hash contract explicit at the boundary rather than silently
    collapsing ``None`` and ``""`` to the same digest.
    """
    canon = []
    for i, part in enumerate(parts):
        if part is None:
            raise TypeError(
                f"normalize() rejects None at position {i}; "
                "convert Optional[str] to '' at the call site"
            )
        canon.append(unicodedata.normalize("NFC", part).strip())
    return _SEPARATOR.join(canon)


def content_hash(*parts: str) -> str:
    """Return ``sha256(normalize(*parts))`` as a 64-char lowercase hex digest.

    Propagates :class:`TypeError` from :func:`normalize` unchanged when any
    part is ``None``.
    """
    return hashlib.sha256(normalize(*parts).encode("utf-8")).hexdigest()
