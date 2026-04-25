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


def test_first_non_empty_rejects_lone_surrogate() -> None:
    """Lone UTF-16 surrogate (U+D800) raises ValueError at normalize boundary."""
    payload = {"body": "hello\ud800world"}
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
