"""v0.1.0-alpha Exit Gate: exhaustive crosswalk coverage over the v0.5 API surface.

This test replaces the narrow spot-checks in ``test_mock_and_seed.py`` with a
drift-safe audit: every ``Intent.*`` and ``RetrieveKind.*`` literal shipped in
v0.5 must resolve to a QueryType (except ``Intent.FALLBACK`` which must stay
fail-closed). Every QueryType must be produced by at least one legacy key.

When the next release adds a new Intent or RetrieveKind without updating
CROSSWALK_SEED, this module fails loud — so the crosswalk can never silently
lose coverage as the API evolves.
"""

from __future__ import annotations

import pytest

from parallax.retrieval.contracts import Intent
from parallax.router.crosswalk_seed import CROSSWALK_SEED, UnroutableQueryError, resolve
from parallax.router.types import QueryType
from parallax.server.schemas import RETRIEVE_KINDS

# ---------------------------------------------------------------------------
# Helpers — reconstruct the canonical legacy key set from v0.5 API surface.
# ---------------------------------------------------------------------------


def _intent_keys_except_fallback() -> list[str]:
    return [f"Intent.{m.name}" for m in Intent if m is not Intent.FALLBACK]


def _retrieve_kind_keys() -> list[str]:
    return [f"RetrieveKind.{k}" for k in RETRIEVE_KINDS]


def _seed_intent_keys() -> set[str]:
    return {k for k in CROSSWALK_SEED if k.startswith("Intent.")}


def _seed_retrieve_kind_keys() -> set[str]:
    return {k for k in CROSSWALK_SEED if k.startswith("RetrieveKind.")}


# ---------------------------------------------------------------------------
# Exhaustive resolve coverage — every v0.5 legacy key maps to a QueryType.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("legacy_key", _intent_keys_except_fallback())
def test_every_intent_except_fallback_resolves(legacy_key: str) -> None:
    result = resolve(legacy_key)
    assert isinstance(result, QueryType), (
        f"{legacy_key} resolved to {result!r} (expected a QueryType)"
    )


@pytest.mark.parametrize("legacy_key", _retrieve_kind_keys())
def test_every_retrieve_kind_resolves(legacy_key: str) -> None:
    result = resolve(legacy_key)
    assert isinstance(result, QueryType), (
        f"{legacy_key} resolved to {result!r} (expected a QueryType)"
    )


def test_intent_fallback_stays_unroutable() -> None:
    """Intent.FALLBACK is deliberately unmapped — resolve must fail closed."""
    with pytest.raises(UnroutableQueryError):
        resolve("Intent.FALLBACK")


# ---------------------------------------------------------------------------
# Drift detection — seed keys and v0.5 API surface must agree exactly.
# ---------------------------------------------------------------------------


def test_seed_intent_keys_match_v05_intent_enum() -> None:
    """If a new Intent member lands without a seed entry, this fails."""
    expected = set(_intent_keys_except_fallback())
    actual = _seed_intent_keys()
    missing_from_seed = expected - actual
    extra_in_seed = actual - expected
    assert not missing_from_seed, (
        f"Intent members not mapped in CROSSWALK_SEED: {sorted(missing_from_seed)}"
    )
    assert not extra_in_seed, (
        f"CROSSWALK_SEED contains Intent keys not in v0.5 Intent enum: "
        f"{sorted(extra_in_seed)}"
    )


def test_seed_retrieve_kind_keys_match_v05_literal() -> None:
    """If RETRIEVE_KINDS grows without a seed entry, this fails."""
    expected = set(_retrieve_kind_keys())
    actual = _seed_retrieve_kind_keys()
    missing_from_seed = expected - actual
    extra_in_seed = actual - expected
    assert not missing_from_seed, (
        f"RetrieveKind literals not mapped in CROSSWALK_SEED: "
        f"{sorted(missing_from_seed)}"
    )
    assert not extra_in_seed, (
        f"CROSSWALK_SEED contains RetrieveKind keys not in v0.5 "
        f"RETRIEVE_KINDS: {sorted(extra_in_seed)}"
    )


# ---------------------------------------------------------------------------
# Orphan detection — every QueryType value must be produced by the seed.
# ---------------------------------------------------------------------------


def test_every_query_type_is_produced_by_at_least_one_legacy_key() -> None:
    produced = set(CROSSWALK_SEED.values())
    missing = set(QueryType) - produced
    assert not missing, (
        f"QueryType values with no legacy key in CROSSWALK_SEED: "
        f"{sorted(v.value for v in missing)}"
    )


# ---------------------------------------------------------------------------
# Cardinality guardrails — hard-coded sizes lock v0.1 contract in place.
# ---------------------------------------------------------------------------


def test_v05_full_dataset_size() -> None:
    """11 legacy keys = 5 non-fallback Intent + 6 RetrieveKind."""
    assert len(_intent_keys_except_fallback()) == 5
    assert len(_retrieve_kind_keys()) == 6
    assert len(CROSSWALK_SEED) == 11


def test_query_type_closed_set_is_five() -> None:
    assert len(list(QueryType)) == 5
