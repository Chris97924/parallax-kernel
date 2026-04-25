"""Tests for parallax.router.normalize — Lane D-3 US-D3-04 helper.

PRD addendum AC coverage:
- declared-order precedence (first key in tuple wins)
- None and '' treated as missing → fall through
- non-str non-None raises ValueError (numeric 0, dict, list, bool)
- lone surrogate raises ValueError at boundary
- missing required raises ValueError listing alias set
- _coerce_optional_float type rules (None pass-through, bool reject, int/float OK)
"""

from __future__ import annotations

import pytest

from parallax.router.normalize import _coerce_optional_float, _first_non_empty

# --- _first_non_empty: precedence ------------------------------------------


def test_first_non_empty_first_key_wins() -> None:
    """Declared-order precedence: first key in tuple wins, even if later keys also resolve."""
    payload = {"body": "first", "object_": "second", "object": "third"}
    keys = ("body", "object_", "object")
    assert _first_non_empty(payload, keys, field="memory.body") == "first"


def test_first_non_empty_falls_through_to_second() -> None:
    """If first key absent, second key is used."""
    payload = {"object_": "second", "object": "third"}
    keys = ("body", "object_", "object")
    assert _first_non_empty(payload, keys, field="memory.body") == "second"


def test_first_non_empty_falls_through_to_last() -> None:
    """All but last key absent → last key used."""
    payload = {"object": "last"}
    keys = ("body", "object_", "object")
    assert _first_non_empty(payload, keys, field="memory.body") == "last"


# --- _first_non_empty: empty semantics -------------------------------------


def test_first_non_empty_none_falls_through() -> None:
    """None at first key → fall through to next (None == missing)."""
    payload = {"body": None, "object_": "real"}
    assert _first_non_empty(payload, ("body", "object_"), field="memory.body") == "real"


def test_first_non_empty_empty_string_falls_through() -> None:
    """Empty string at first key → fall through to next ('' == missing)."""
    payload = {"body": "", "object_": "real"}
    assert _first_non_empty(payload, ("body", "object_"), field="memory.body") == "real"


def test_first_non_empty_none_and_empty_both_fall_through() -> None:
    """Mix of None and '' → fall through both, find third."""
    payload = {"body": None, "object_": "", "object": "real"}
    keys = ("body", "object_", "object")
    assert _first_non_empty(payload, keys, field="memory.body") == "real"


# --- _first_non_empty: type rejection --------------------------------------


@pytest.mark.parametrize(
    "bad_value,type_name",
    [
        (0, "int"),
        (1.5, "float"),
        ([], "list"),
        ({}, "dict"),
        (("tup",), "tuple"),
        (b"bytes", "bytes"),
    ],
)
def test_first_non_empty_rejects_non_str(bad_value: object, type_name: str) -> None:
    """Non-str non-None values raise ValueError with field name + type."""
    payload = {"body": bad_value}
    with pytest.raises(ValueError, match=r"non-str") as exc:
        _first_non_empty(payload, ("body",), field="memory.body")
    assert "memory.body" in str(exc.value)
    assert type_name in str(exc.value)


def test_first_non_empty_rejects_bool_at_first_key() -> None:
    """bool is int subclass — must still be rejected as non-str."""
    payload = {"body": True}
    with pytest.raises(ValueError, match=r"non-str"):
        _first_non_empty(payload, ("body",), field="memory.body")


# --- _first_non_empty: Unicode validation ----------------------------------


@pytest.mark.parametrize(
    "surrogate",
    ["\ud800", "\udbff", "\udc00", "\udfff"],
    ids=["high-low-D800", "high-high-DBFF", "low-low-DC00", "low-high-DFFF"],
)
def test_first_non_empty_rejects_lone_surrogate(surrogate: str) -> None:
    """All four corner unpaired surrogate codepoints raise ValueError."""
    payload = {"body": f"hello{surrogate}world"}
    with pytest.raises(ValueError, match=r"surrogate"):
        _first_non_empty(payload, ("body",), field="memory.body")


def test_first_non_empty_accepts_paired_surrogates_via_supplementary() -> None:
    """Properly-encoded supplementary plane chars (e.g. emoji) pass through."""
    payload = {"body": "hello \U0001f600 world"}
    result = _first_non_empty(payload, ("body",), field="memory.body")
    assert result == "hello \U0001f600 world"


# --- _first_non_empty: missing required ------------------------------------


def test_first_non_empty_missing_raises_with_alias_list() -> None:
    """All keys missing → ValueError with field name and full alias list."""
    payload = {"unrelated": "x"}
    keys = ("body", "object_", "object", "payload_text")
    with pytest.raises(ValueError) as exc:
        _first_non_empty(payload, keys, field="memory.body")
    msg = str(exc.value)
    assert "memory.body" in msg
    for k in keys:
        assert k in msg


def test_first_non_empty_all_keys_none_or_empty_raises() -> None:
    """All keys present but all None/'' → still missing."""
    payload = {"body": None, "object_": "", "object": None}
    keys = ("body", "object_", "object")
    with pytest.raises(ValueError):
        _first_non_empty(payload, keys, field="memory.body")


# --- _coerce_optional_float ------------------------------------------------


def test_coerce_optional_float_none_passes_through() -> None:
    assert _coerce_optional_float(None, field="claim.confidence") is None


@pytest.mark.parametrize("value", [0, 1, 42, -7])
def test_coerce_optional_float_int_to_float(value: int) -> None:
    result = _coerce_optional_float(value, field="claim.confidence")
    assert isinstance(result, float)
    assert result == float(value)


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0, -1.5])
def test_coerce_optional_float_float_passes_through(value: float) -> None:
    assert _coerce_optional_float(value, field="claim.confidence") == value


@pytest.mark.parametrize("bad", [True, False])
def test_coerce_optional_float_rejects_bool(bad: bool) -> None:
    with pytest.raises(ValueError, match=r"bool not accepted"):
        _coerce_optional_float(bad, field="claim.confidence")


@pytest.mark.parametrize("bad", ["0.5", [], {}, b"\x00"])
def test_coerce_optional_float_rejects_other_types(bad: object) -> None:
    with pytest.raises(ValueError, match=r"expected float"):
        _coerce_optional_float(bad, field="claim.confidence")


# --- HIGH-1: NaN / inf rejection -------------------------------------------


@pytest.mark.parametrize(
    "non_finite",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "+inf", "-inf"],
)
def test_coerce_optional_float_rejects_non_finite(non_finite: float) -> None:
    """NaN, +inf, -inf must raise — confidence-like fields cannot store these."""
    with pytest.raises(ValueError, match=r"non-finite"):
        _coerce_optional_float(non_finite, field="claim.confidence")


# --- HIGH-2: custom __eq__ bypass guard ------------------------------------


def test_first_non_empty_rejects_eq_empty_object() -> None:
    """An object whose __eq__ returns True for '' must NOT silently fall through.

    Before the fix, ``value == ""`` was evaluated via the value's ``__eq__``,
    so a class returning ``True`` for ``== ""`` skipped the type-rejection
    branch and the loop fell through to the next alias. The fix is to check
    ``isinstance(value, str)`` before treating ``""`` as missing.
    """

    class TrueEq:
        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return 0

    payload = {"body": TrueEq()}
    with pytest.raises(ValueError, match=r"non-str"):
        _first_non_empty(payload, ("body",), field="memory.body")


# --- New: optional default parameter (HIGH-4 enabler) ----------------------


def test_first_non_empty_default_returns_on_missing() -> None:
    """default param: returns the default when no key resolves (no exception)."""
    payload: dict[str, str] = {}
    result = _first_non_empty(payload, ("title", "name"), field="memory.title", default=None)
    assert result is None


def test_first_non_empty_default_returns_on_all_none_or_empty() -> None:
    """default param: all keys present but None/'' → returns default, no raise."""
    payload = {"title": None, "name": ""}
    result = _first_non_empty(payload, ("title", "name"), field="memory.title", default=None)
    assert result is None


def test_first_non_empty_default_does_not_swallow_type_error() -> None:
    """default param is for missing-only — type errors must still propagate."""
    payload = {"title": 0}
    with pytest.raises(ValueError, match=r"non-str"):
        _first_non_empty(payload, ("title", "name"), field="memory.title", default=None)


def test_first_non_empty_default_does_not_swallow_lone_surrogate() -> None:
    """default param is for missing-only — surrogate errors must still propagate."""
    payload = {"title": "ok\ud800broken"}
    with pytest.raises(ValueError, match=r"surrogate"):
        _first_non_empty(payload, ("title", "name"), field="memory.title", default=None)


def test_first_non_empty_default_returns_value_on_hit() -> None:
    """default is only used when no alias resolves; otherwise behavior unchanged."""
    payload = {"title": "real"}
    assert (
        _first_non_empty(payload, ("title", "name"), field="memory.title", default=None) == "real"
    )
