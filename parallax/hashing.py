"""Canonical normalization + sha256 hashing for Parallax content_hash columns.

Schema contract (E:/Parallax/schema.sql):
    memories.content_hash = sha256(normalize(title || summary || vault_path))
    claims.content_hash   = sha256(normalize(subject || predicate || object
                                             || source_id || user_id))
                            # ADR-005, v0.5.0-pre1 — hash is user-scoped

Both call sites funnel through this single module so dedup semantics stay
identical across writers, migrations, and verifiers.

v0.4.0 None-sentinel contract:
    ``normalize`` accepts ``str | None``. ``None`` values are encoded with the
    internal sentinel ``\\x00\\x00NONE\\x00\\x00`` before join so that
    ``normalize(None)`` and ``normalize("")`` produce distinct canonical
    strings (and therefore distinct sha256 digests). Prior to v0.4.0
    ``normalize(None)`` raised ``TypeError`` and callers converted
    ``None`` → ``""`` themselves, which collapsed the two values onto the
    same hash (see ADR-001 / progress.txt v0.4.0 entry for rationale).

    The sentinel is a control-character byte sequence that cannot appear in
    a valid UTF-8 string produced by NFC normalization of user input (NFC
    preserves NUL but NUL-delimited user text is never accepted by the
    ingest boundary). The sentinel is consumed only inside ``normalize`` and
    never reaches storage.
"""

from __future__ import annotations

import hashlib
import unicodedata

__all__ = ["normalize", "content_hash"]

_SEPARATOR = "||"
_NONE_SENTINEL = "\x00\x00NONE\x00\x00"


def normalize(*parts: str | None) -> str:
    """Return the canonical string form of ``parts`` for hashing.

    Each ``str`` part is NFC-normalized and stripped of surrounding
    whitespace. Each ``None`` part is encoded as :data:`_NONE_SENTINEL` so
    that ``None`` and ``""`` produce distinct outputs. Parts are joined
    with ``"||"`` to match the schema comments. Internal whitespace is
    preserved.
    """
    canon: list[str] = []
    for part in parts:
        if part is None:
            canon.append(_NONE_SENTINEL)
        else:
            canon.append(unicodedata.normalize("NFC", part).strip())
    return _SEPARATOR.join(canon)


def content_hash(*parts: str | None) -> str:
    """Return ``sha256(normalize(*parts))`` as a 64-char lowercase hex digest."""
    return hashlib.sha256(normalize(*parts).encode("utf-8")).hexdigest()
