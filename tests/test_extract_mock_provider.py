"""Tests for parallax.extract.providers.mock.MockProvider."""

from __future__ import annotations

import pytest

from parallax.extract import Provider, RawClaim
from parallax.extract.providers.mock import MockProvider


def _raw(entity: str = "x") -> RawClaim:
    return RawClaim(
        entity=entity,
        claim_text="c",
        polarity=1,
        confidence=0.9,
        claim_type="feature",
        evidence="e",
    )


class TestMockProvider:
    def test_default_returns_empty(self) -> None:
        p = MockProvider()
        assert p.extract_claims("anything") == []

    def test_preset_claims_returned_verbatim(self) -> None:
        preset = [_raw("a"), _raw("b")]
        p = MockProvider(claims=preset)
        out = p.extract_claims("hi")
        assert out == preset
        assert out is not preset  # defensive copy

    def test_fn_mode_receives_text(self) -> None:
        seen: list[str] = []

        def fn(text: str) -> list[RawClaim]:
            seen.append(text)
            return [_raw(text)]

        p = MockProvider(fn=fn)
        out = p.extract_claims("hello")
        assert seen == ["hello"]
        assert out == [_raw("hello")]

    def test_calls_recorded(self) -> None:
        p = MockProvider()
        p.extract_claims("t1")
        p.extract_claims("t2")
        assert p.calls == ["t1", "t2"]

    def test_satisfies_protocol(self) -> None:
        assert isinstance(MockProvider(), Provider)

    def test_claims_and_fn_both_raises(self) -> None:
        with pytest.raises(ValueError, match="either claims= or fn="):
            MockProvider(claims=[_raw()], fn=lambda _t: [])
