"""Nightly integration — hits a real OpenRouter endpoint.

Skipped by default. Run with::

    OPENROUTER_API_KEY=sk-... python -m pytest tests/integration/ \\
        -m llm_integration -q

The assertion is intentionally loose (``list[RawClaim]``) because the real
LLM can legitimately return zero claims for a short / neutral prompt; the
point of this test is that the provider round-trips (auth + HTTP + JSON
parse), not that any specific claim was extracted.
"""

from __future__ import annotations

import os

import pytest

from parallax.extract import RawClaim


@pytest.mark.llm_integration
def test_openrouter_roundtrip() -> None:
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")

    # Import inside the test so the module isn't loaded during default
    # `pytest -m 'not llm_integration'` collection; httpx stays off the
    # import graph unless we explicitly run this.
    from parallax.extract.providers.openrouter import OpenRouterProvider

    provider = OpenRouterProvider(
        model=os.environ.get(
            "OPENROUTER_MODEL", "anthropic/claude-3.5-haiku"
        )
    )
    claims = provider.extract_claims(
        "Bitcoin is volatile and sometimes used as a store of value."
    )
    assert isinstance(claims, list)
    for c in claims:
        assert isinstance(c, RawClaim)
