"""Field normalization helpers for the MEMORY_ROUTER (Lane D-3).

Single source of truth for alias precedence — both the ingest path
(:mod:`parallax.router.real_adapter` ``RealMemoryRouter.ingest``) and the
read-side projection (``RealMemoryRouter.query`` DTO ``body`` field) MUST
use these helpers to avoid divergence (PRD addendum, Sonnet Critic xcouncil
Round 2).

Public surface (re-exported via ``__all__``): ``_first_non_empty``,
``_coerce_optional_float``. The leading underscore matches the literal
naming in the signed PRD addendum and signals "package-internal helper —
no stability guarantee for callers outside ``parallax.router``".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["_first_non_empty", "_coerce_optional_float"]


def _first_non_empty(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    field: str,
) -> str:
    """Return the first non-empty ``str`` value from ``payload[k]`` for ``k in keys``.

    PRD-addendum semantics:

    * **Declared-order precedence** — first key in ``keys`` whose value is a
      non-empty ``str`` wins; later aliases are not consulted.
    * **Empty semantics** — both ``None`` and ``''`` are treated as missing
      and fall through to the next alias.
    * **Type rule** — non-``str`` non-``None`` values raise ``ValueError``
      with the field name and actual type. Empty containers and numeric
      ``0`` are NOT silently coerced via ``str()``; they raise.
    * **Unicode validation** — values containing unpaired surrogates raise
      ``ValueError`` at this boundary, before any sqlite encode would crash
      with a less helpful traceback.
    * **Required-field semantics** — exhausting all keys raises
      ``ValueError`` listing the alias set so the caller sees what was
      tried.

    Args:
        payload: caller-supplied mapping (typically ``IngestRequest.payload``).
        keys: declared-order alias precedence tuple.
        field: canonical field name for error messages, e.g. ``"memory.body"``.

    Returns:
        The first non-empty ``str`` value found.

    Raises:
        ValueError: on type mismatch, unpaired surrogate, or when no key
            resolves to a non-empty ``str``.
    """
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"{field}: alias {key!r} resolved to non-str value of type "
                f"{type(value).__name__}; explicit str required"
            )
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"{field}: alias {key!r} contains unpaired surrogate "
                f"({exc.reason}); cannot persist"
            ) from exc
        return value
    raise ValueError(
        f"{field}: required field missing — none of {list(keys)!r} resolved "
        f"to a non-empty str in payload"
    )


def _coerce_optional_float(value: Any, *, field: str) -> float | None:
    """Coerce a numeric-or-``None`` value to ``float | None``.

    ``None`` passes through. ``int`` and ``float`` are accepted (``int`` is
    promoted to ``float``). ``bool`` is rejected explicitly because it is an
    ``int`` subclass and silently coercing ``True`` to ``1.0`` would mask
    caller bugs. Strings, bytes, and containers raise.

    Args:
        value: caller-supplied value, typically from a payload mapping.
        field: canonical field name for error messages.

    Returns:
        ``None`` if ``value`` is ``None``; otherwise ``float(value)``.

    Raises:
        ValueError: if ``value`` is ``bool`` or any non-numeric type.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}: bool not accepted; use float or None")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"{field}: expected float|int|None, got {type(value).__name__}")
