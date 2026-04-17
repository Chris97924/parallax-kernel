"""RED-phase pytest suite for parallax.hashing.

Spec source: E:/Parallax/schema.sql comments:
  memories.content_hash  = sha256(normalize(title||summary||vault_path))
  claims.content_hash    = sha256(normalize(subject||predicate||object||source_id))

This suite pins the public contract before any implementation exists.
"""

from __future__ import annotations

import hashlib
import unicodedata

import pytest

from parallax.hashing import content_hash, normalize


class TestNormalize:
    def test_returns_str(self) -> None:
        assert isinstance(normalize("a", "b"), str)

    def test_deterministic(self) -> None:
        assert normalize("x", "y", "z") == normalize("x", "y", "z")

    def test_joins_with_double_pipe(self) -> None:
        # Matches schema comments: title||summary||vault_path
        assert normalize("a", "b", "c") == "a||b||c"

    def test_nfc_unicode_composition(self) -> None:
        # Decomposed "é" (e + combining acute) must equal composed "é"
        decomposed = "e\u0301"
        composed = "\u00e9"
        assert decomposed != composed  # precondition: inputs differ byte-wise
        assert normalize(decomposed) == normalize(composed)

    def test_nfc_unicode_cjk(self) -> None:
        # Schema is used for Chinese content; NFC must leave already-composed CJK unchanged
        assert normalize("記憶") == "記憶"

    def test_trims_surrounding_whitespace(self) -> None:
        assert normalize("  hello  ", "\tworld\n") == "hello||world"

    def test_preserves_internal_whitespace(self) -> None:
        assert normalize("hello world") == "hello world"

    def test_none_is_empty(self) -> None:
        assert normalize(None) == ""
        assert normalize("a", None, "c") == "a||||c"

    def test_empty_call_returns_empty(self) -> None:
        assert normalize() == ""

    def test_single_part_no_separator(self) -> None:
        assert normalize("only") == "only"

    def test_does_not_mutate_inputs(self) -> None:
        parts = ["  one  ", "two"]
        snapshot = list(parts)
        normalize(*parts)
        assert parts == snapshot


class TestContentHash:
    def test_returns_64_char_lowercase_hex(self) -> None:
        h = content_hash("a", "b", "c")
        assert isinstance(h, str)
        assert len(h) == 64
        assert h == h.lower()
        int(h, 16)  # must be valid hex

    def test_compositional_contract(self) -> None:
        # content_hash == sha256(normalize(*parts).encode('utf-8')).hexdigest()
        parts = ("title", "summary", "vault_path")
        expected = hashlib.sha256(normalize(*parts).encode("utf-8")).hexdigest()
        assert content_hash(*parts) == expected

    def test_deterministic(self) -> None:
        assert content_hash("x", "y") == content_hash("x", "y")

    def test_different_inputs_different_hash(self) -> None:
        assert content_hash("a", "b") != content_hash("a", "c")
        assert content_hash("a", "b") != content_hash("b", "a")

    def test_nfc_equivalence(self) -> None:
        # Logically equal strings (NFC-equivalent) must hash identically.
        decomposed = unicodedata.normalize("NFD", "café")
        composed = unicodedata.normalize("NFC", "café")
        assert decomposed != composed
        assert content_hash(decomposed) == content_hash(composed)

    def test_whitespace_insensitive_on_edges(self) -> None:
        assert content_hash("  hi  ") == content_hash("hi")

    def test_none_treated_as_empty(self) -> None:
        assert content_hash("a", None, "c") == content_hash("a", "", "c")

    def test_schema_memories_example(self) -> None:
        # memories: sha256(normalize(title||summary||vault_path))
        h = content_hash("My Title", "A summary.", "users/chris/memories/foo.md")
        assert len(h) == 64

    def test_schema_claims_example(self) -> None:
        # claims: sha256(normalize(subject||predicate||object||source_id))
        h = content_hash("Chris", "likes", "coffee", "direct:chris")
        assert len(h) == 64

    def test_order_matters(self) -> None:
        assert content_hash("a", "b", "c") != content_hash("c", "b", "a")

    @pytest.mark.parametrize(
        "parts",
        [
            ("simple",),
            ("", "", ""),
            ("中文", "測試", "雙管道||tricky"),
            (None, None),
            ("a" * 1000, "b" * 1000),
        ],
    )
    def test_always_produces_valid_sha256(self, parts: tuple) -> None:
        h = content_hash(*parts)
        assert len(h) == 64
        int(h, 16)
