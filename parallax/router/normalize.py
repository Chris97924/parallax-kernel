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

import math
from collections.abc import Mapping
from typing import Any, Final

__all__ = ["_first_non_empty", "_coerce_optional_float"]

# Sentinel for the optional-default form of _first_non_empty. A bare
# ``object()`` is enough; the only check is identity.
_MISSING: Final = object()


def _first_non_empty(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
    *,
    field: str,
    default: Any = _MISSING,
) -> Any:
    """Return the first non-empty ``str`` value from ``payload[k]`` for ``k in keys``.

    PRD-addendum semantics:

    * **Declared-order precedence** — first key in ``keys`` whose value is a
      non-empty ``str`` wins; later aliases are not consulted.
    * **Empty semantics** — both ``None`` and ``''`` are treated as missing
      and fall through to the next alias. The empty-``str`` test is gated
      behind ``isinstance(value, str)`` so a class with a custom ``__eq__``
      that returns ``True`` for ``""`` cannot bypass the type-rejection
      branch.
    * **Type rule** — non-``str`` non-``None`` values raise ``ValueError``
      with the field name and actual type. Empty containers, numeric ``0``,
      and ``bool`` all raise.
    * **Unicode validation** — values containing unpaired surrogates (any
      codepoint in ``U+D800``–``U+DFFF`` outside a valid pair) raise
      ``ValueError`` at this boundary, before any sqlite encode would crash
      with a less helpful traceback.
    * **Required vs. optional** — when ``default`` is omitted, exhausting
      all keys raises ``ValueError`` listing the alias set. When
      ``default`` is supplied, that value is returned instead. **Type and
      surrogate errors always propagate** — the ``default`` argument is
      ONLY consulted when no key resolves; a malformed value never falls
      back silently.

    Args:
        payload: caller-supplied mapping (typically ``IngestRequest.payload``).
        keys: declared-order alias precedence tuple.
        field: canonical field name for error messages, e.g. ``"memory.body"``.
        default: optional fallback returned when no key resolves. The
            sentinel ``_MISSING`` (the unset state) signals "required" and
            triggers the listing-style ``ValueError``.

    Returns:
        The first non-empty ``str`` value found, or ``default`` when set.

    Raises:
        ValueError: on type mismatch, unpaired surrogate, or — when
            ``default`` is unset — when no key resolves to a non-empty
            ``str``.
    """
    for key in keys:
        value = payload.get(key, _MISSING)
        if value is _MISSING or value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"{field}: alias {key!r} resolved to non-str value of type "
                f"{type(value).__name__}; explicit str required"
            )
        if value == "":
            continue
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"{field}: alias {key!r} contains unpaired surrogate "
                f"({exc.reason}); cannot persist"
            ) from exc
        return value
    if default is _MISSING:
        raise ValueError(
            f"{field}: required field missing — none of {list(keys)!r} resolved "
            f"to a non-empty str in payload"
        )
    return default


def _coerce_optional_float(value: Any, *, field: str) -> float | None:
    """Coerce a numeric-or-``None`` value to ``float | None``.

    ``None`` passes through. ``int`` and ``float`` are accepted (``int`` is
    promoted to ``float``). ``bool`` is rejected explicitly because it is an
    ``int`` subclass and silently coercing ``True`` to ``1.0`` would mask
    caller bugs. ``NaN``, ``+inf``, and ``-inf`` are rejected for the same
    reason — confidence-like fields cannot meaningfully store these and
    silently persisting them masks data bugs. Strings, bytes, and
    containers raise.

    Args:
        value: caller-supplied value, typically from a payload mapping.
        field: canonical field name for error messages.

    Returns:
        ``None`` if ``value`` is ``None``; otherwise ``float(value)``.

    Raises:
        ValueError: if ``value`` is ``bool``, a non-finite ``float``
            (``NaN`` / ``inf``), or any non-numeric type.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field}: bool not accepted; use float or None")
    if isinstance(value, (int, float)):
        as_float = float(value)
        if not math.isfinite(as_float):
            raise ValueError(
                f"{field}: non-finite float {value!r} not accepted "
                f"(NaN / inf cannot be persisted)"
            )
        return as_float
    raise ValueError(f"{field}: expected float|int|None, got {type(value).__name__}")
