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


def normalize(*parts: str | None) -> str:
    """Return the canonical string form of ``parts`` for hashing.

    Each part is NFC-normalized, stripped of surrounding whitespace, and
    ``None`` is treated as ``""``. Parts are joined with ``"||"`` to match the
    schema comments. Internal whitespace is preserved.
    """
    canon = [
        unicodedata.normalize("NFC", part).strip() if part is not None else ""
        for part in parts
    ]
    return _SEPARATOR.join(canon)


def content_hash(*parts: str | None) -> str:
    """Return ``sha256(normalize(*parts))`` as a 64-char lowercase hex digest."""
    return hashlib.sha256(normalize(*parts).encode("utf-8")).hexdigest()
