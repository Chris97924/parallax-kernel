"""Thin Gemini client wrapper for LongMemEval runs.

Loads ``GEMINI_API_KEY`` from the environment (``.env`` via python-dotenv).
Exposes a single :func:`call` that wraps ``google-genai`` with:

* A tight ``(model, system, user)`` surface — no drift of kwargs.
* Retry on transient 429 / 500 with exponential backoff.
* Token-count hint returned alongside the text so the runner can log cost.

The key is resolved at call-time (not import-time) so a missing key does
not crash the whole module — critical when tests stub the SDK.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env from Parallax repo root so `GEMINI_API_KEY` is picked up when
# this module is imported from anywhere in the repo.
load_dotenv()


@dataclass(frozen=True)
class GeminiResult:
    text: str
    prompt_tokens: int
    output_tokens: int
    model: str


def _collect_keys() -> list[str]:
    keys: list[str] = []
    primary = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if primary:
        keys.append(primary)
    for i in range(2, 10):
        k = os.environ.get(f"GEMINI_API_KEY_{i}")
        if k:
            keys.append(k)
    return keys


_KEYS = _collect_keys()
_key_cycle = itertools.cycle(_KEYS) if _KEYS else None
_key_lock = threading.Lock()


def _next_key() -> str:
    if not _KEYS:
        raise RuntimeError("no GEMINI_API_KEY* env vars set")
    with _key_lock:
        return next(_key_cycle)  # type: ignore[arg-type]


def _client_for(key: str) -> Any:
    from google import genai  # type: ignore[import-not-found]

    return genai.Client(api_key=key)


def call(
    *,
    model: str,
    user: str,
    system: str | None = None,
    temperature: float = 0.1,
    max_output_tokens: int = 2048,
    max_retries: int = 4,
    backoff_base: float = 2.0,
) -> GeminiResult:
    """Call a Gemini model with retry on 429/500.

    The SDK is imported lazily inside :func:`_client` so that tests that
    monkey-patch ``google.genai`` work without real network.
    """
    from google.genai import types as gtypes  # type: ignore[import-not-found]

    config = gtypes.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        system_instruction=system,
    )

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        client = _client_for(_next_key())
        try:
            resp = client.models.generate_content(
                model=model, contents=user, config=config
            )
            usage = getattr(resp, "usage_metadata", None)
            pt = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
            ot = int(
                getattr(usage, "candidates_token_count", 0) or 0
            ) if usage else 0
            return GeminiResult(
                text=resp.text or "",
                prompt_tokens=pt,
                output_tokens=ot,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            retryable = any(
                s in msg
                for s in ("429", "500", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE")
            )
            if attempt >= max_retries or not retryable:
                raise
            wait = backoff_base**attempt
            logger.warning(
                "gemini %s attempt=%d retrying in %.1fs: %s",
                model,
                attempt,
                wait,
                msg[:160],
            )
            time.sleep(wait)
            last_exc = exc

    raise RuntimeError(
        f"gemini call failed after {max_retries} retries: {last_exc}"
    )
