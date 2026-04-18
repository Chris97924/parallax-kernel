"""Tests for parallax.extract.extractor."""

from __future__ import annotations

import pytest

from parallax.extract import RawClaim, extract_claims
from parallax.extract.extractor import chunk_text, render_prompt
from parallax.extract.providers.mock import MockProvider


def _raw() -> RawClaim:
    return RawClaim(
        entity="bitcoin",
        claim_text="it is volatile",
        polarity=-1,
        confidence=0.8,
        claim_type="risk",
        evidence="e",
    )


class TestExtractClaims:
    def test_empty_string_shortcut(self) -> None:
        p = MockProvider(claims=[_raw()])
        assert extract_claims("", provider=p) == []
        assert p.calls == []

    def test_whitespace_shortcut(self) -> None:
        p = MockProvider(claims=[_raw()])
        assert extract_claims("   \n  ", provider=p) == []
        assert p.calls == []

    def test_delegates_to_provider(self) -> None:
        p = MockProvider(claims=[_raw()])
        out = extract_claims("some text", provider=p)
        assert out == [_raw()]
        assert p.calls == ["some text"]

    def test_non_callable_attr_raises(self) -> None:
        # duck-typed: presence of a non-callable attribute still trips guard
        class BadProvider:
            extract_claims = 42  # type: ignore[assignment]

        with pytest.raises(TypeError, match="callable 'extract_claims'"):
            extract_claims("x", provider=BadProvider())  # type: ignore[arg-type]

    def test_missing_attr_raises_attribute_error(self) -> None:
        class NotAProvider:
            pass

        # no extract_claims attr at all → Python-native AttributeError-style
        # handling via getattr returns None → TypeError from guard
        with pytest.raises(TypeError, match="callable 'extract_claims'"):
            extract_claims("x", provider=NotAProvider())  # type: ignore[arg-type]


class TestChunkText:
    def test_short_text_returns_single_chunk(self) -> None:
        assert chunk_text("hello world") == ["hello world"]

    def test_chunk_boundary_overlap(self) -> None:
        # max_tokens=10 words~=7; overlap=4 words~=3 => two overlapping chunks
        text = " ".join(str(i) for i in range(30))
        chunks = chunk_text(text, max_tokens=10, overlap=4)
        assert len(chunks) >= 2
        # each chunk is a non-empty whitespace-split sequence
        for c in chunks:
            assert c.split()

    def test_all_text_eventually_covered(self) -> None:
        text = " ".join(str(i) for i in range(50))
        chunks = chunk_text(text, max_tokens=12, overlap=4)
        joined = " ".join(chunks).split()
        # every original token appears in the union (ignoring duplicates from overlap)
        assert set(joined) >= set(text.split())

    def test_overlap_ge_max_tokens_raises(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            chunk_text("a b c d e", max_tokens=5, overlap=5)


class TestRenderPrompt:
    def test_braces_in_text_do_not_crash(self) -> None:
        # str.format would KeyError on '{foo}'; render_prompt uses str.replace.
        rendered = render_prompt("payload with {foo} and {{bar}} braces")
        assert "payload with {foo} and {{bar}} braces" in rendered

    def test_format_spec_in_text_is_literal(self) -> None:
        # ensure format-spec sequences like '{0:>10}' are not interpreted
        rendered = render_prompt("{0:>10}")
        assert "{0:>10}" in rendered
