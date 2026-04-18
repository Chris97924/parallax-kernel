"""Shared JSON → RawClaim parser for LLM providers.

Both ``OpenRouterProvider`` and ``ClaudeSubprocessProvider`` receive the
LLM output as a plain string and must defensively parse it into
``RawClaim`` values. The parsing rules (fenced-code stripping, confidence
floor, polarity clamping, evidence truncation) are identical across
providers, so they live here.
"""

from __future__ import annotations

import json
import logging

from parallax.extract.types import RawClaim

__all__ = ["parse_claims_json", "MIN_CONFIDENCE"]

MIN_CONFIDENCE = 0.5

_logger = logging.getLogger("parallax.extract.providers")


def parse_claims_json(raw: str) -> list[RawClaim]:
    """Parse an LLM JSON-array response into ``RawClaim`` values.

    Never raises — malformed input, wrong shape, per-item type errors, and
    sub-threshold confidences all silently filter out. Providers rely on
    this to keep the shadow-write path blameless.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.startswith("```")
        ).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _logger.warning("Failed to parse LLM output as JSON: %s", exc)
        return []
    if not isinstance(data, list):
        return []

    claims: list[RawClaim] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
            if confidence < MIN_CONFIDENCE:
                continue
            polarity = int(item.get("polarity", 0))
            if polarity not in (-1, 0, 1):
                polarity = 0
            claims.append(
                RawClaim(
                    entity=str(item.get("entity", "Unknown")).strip(),
                    claim_text=str(item.get("claim_text", "")).strip(),
                    polarity=polarity,
                    confidence=confidence,
                    claim_type=str(item.get("claim_type", "opinion")).strip(),
                    evidence=str(item.get("evidence", "")).strip()[:200],
                )
            )
        except (TypeError, ValueError):
            continue
    return claims
