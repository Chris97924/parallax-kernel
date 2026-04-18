"""OpenRouterProvider — real LLM backend used by the nightly integration test.

Imports ``httpx`` at module level on purpose: this is the reason
``parallax.extract.providers.openrouter`` lives behind the ``[extract]``
extra. Core install never touches this file.
"""

from __future__ import annotations

import logging
import os

import httpx  # type: ignore[import]

from parallax.extract.extractor import render_prompt
from parallax.extract.providers._parse import parse_claims_json
from parallax.extract.types import RawClaim

__all__ = ["OpenRouterProvider"]

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    """Provider backed by OpenRouter's OpenAI-compatible chat completions API.

    ``base_url`` is caller-controlled and not validated. Do **not** wire it
    to untrusted configuration — a malicious value could redirect the
    request at an internal service (SSRF). Pin it in deployment code.
    """

    def __init__(
        self,
        *,
        model: str = "anthropic/claude-3.5-haiku",
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _resolve_key(self) -> str:
        key = self._api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        return key

    def extract_claims(self, text: str) -> list[RawClaim]:
        if not text or not text.strip():
            return []
        key = self._resolve_key()
        prompt = render_prompt(text)
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("OpenRouter request failed: %s", exc)
            return []

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("OpenRouter response shape unexpected: %s", exc)
            return []

        return parse_claims_json(content)
