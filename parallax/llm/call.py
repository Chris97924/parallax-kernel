"""Unified LLM call with SQLite cache, tenacity retry, and fallback model.

Contract rules (ADR-006):

* Every LLM call in Parallax goes through :func:`call`.
* Cache key is deterministic over ``(model, messages, response_schema)`` —
  or the caller-supplied ``cache_key`` when they want to pin a run.
* 429 / rate-limit raises :class:`RateLimitError`; if a ``fallback_model`` is
  supplied, the call is retried once with that model.
* All provider-specific HTTP stays in ``_call_gemini`` / ``_call_anthropic``.
  Callers see a uniform ``dict`` return shape.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import sqlite3
import threading
import time
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


class LLMCallError(RuntimeError):
    """Raised when an LLM call fails in a non-retryable way."""


class RateLimitError(RuntimeError):
    """Raised on 429 / RESOURCE_EXHAUSTED — triggers fallback_model."""


_DEFAULT_CACHE_PATH = pathlib.Path.home() / ".parallax" / "llm_cache.sqlite"
_db_lock = threading.Lock()


def _cache_path() -> pathlib.Path:
    env = os.environ.get("PARALLAX_LLM_CACHE")
    if env:
        return pathlib.Path(env)
    return _DEFAULT_CACHE_PATH


def _connect_cache() -> sqlite3.Connection:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_cache (
            model         TEXT NOT NULL,
            prompt_hash   TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            created_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_cache_model ON llm_cache(model)"
    )
    return conn


def _hash_prompt(
    model: str,
    messages: list[dict],
    response_schema: dict | None,
    cache_key: str | None,
) -> str:
    if cache_key is not None:
        raw = f"{model}::{cache_key}"
    else:
        msg_blob = json.dumps(messages, sort_keys=True, ensure_ascii=False)
        schema_blob = json.dumps(response_schema or {}, sort_keys=True)
        raw = f"{model}::{msg_blob}::{schema_blob}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(conn: sqlite3.Connection, prompt_hash: str) -> dict | None:
    row = conn.execute(
        "SELECT response_json FROM llm_cache WHERE prompt_hash = ?",
        (prompt_hash,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _cache_put(
    conn: sqlite3.Connection,
    model: str,
    prompt_hash: str,
    response: dict,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO llm_cache (model, prompt_hash, response_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            model,
            prompt_hash,
            json.dumps(response, ensure_ascii=False),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )


# ---------- Provider dispatch ------------------------------------------------


def _messages_to_gemini(messages: list[dict]) -> tuple[str | None, str]:
    """Split messages into (system_instruction, user_prompt)."""
    system_parts: list[str] = []
    user_parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            user_parts.append(content)
    system = "\n\n".join(system_parts) if system_parts else None
    user = "\n\n".join(user_parts)
    return system, user


def _call_gemini(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_output_tokens: int,
) -> dict:
    try:
        from google import genai  # type: ignore[import-not-found]
        from google.genai import types as gtypes  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover — SDK missing
        raise LLMCallError(f"google-genai SDK not importable: {exc}") from exc

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise LLMCallError("GEMINI_API_KEY not set")

    system, user = _messages_to_gemini(messages)
    client = genai.Client(api_key=api_key)
    config = gtypes.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        system_instruction=system,
    )
    try:
        resp = client.models.generate_content(model=model, contents=user, config=config)
    except Exception as exc:
        msg = str(exc)
        if any(s in msg for s in ("429", "RESOURCE_EXHAUSTED")):
            raise RateLimitError(msg) from exc
        raise LLMCallError(msg) from exc

    usage = getattr(resp, "usage_metadata", None)
    pt = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    ot = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
    return {
        "text": resp.text or "",
        "raw": {"candidates": str(getattr(resp, "candidates", None))[:400]},
        "model": model,
        "prompt_tokens": pt,
        "completion_tokens": ot,
    }


def _call_anthropic(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_output_tokens: int,
) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMCallError("ANTHROPIC_API_KEY not set; cannot use Claude fallback")
    try:
        import anthropic  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover
        raise LLMCallError(f"anthropic SDK not importable: {exc}") from exc

    client = anthropic.Anthropic(api_key=api_key)
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    user_messages = [
        {"role": m.get("role", "user"), "content": m.get("content", "")}
        for m in messages
        if m.get("role") != "system"
    ]
    try:
        resp = client.messages.create(
            model=model,
            messages=user_messages,
            system="\n\n".join(system_parts) if system_parts else None,
            temperature=temperature,
            max_tokens=max_output_tokens,
        )
    except Exception as exc:
        msg = str(exc)
        if "429" in msg or "rate" in msg.lower():
            raise RateLimitError(msg) from exc
        raise LLMCallError(msg) from exc

    text = "".join(
        getattr(block, "text", "") for block in getattr(resp, "content", [])
    )
    return {
        "text": text,
        "raw": {"stop_reason": getattr(resp, "stop_reason", None)},
        "model": model,
        "prompt_tokens": getattr(resp.usage, "input_tokens", 0),
        "completion_tokens": getattr(resp.usage, "output_tokens", 0),
    }


def _dispatch(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_output_tokens: int,
) -> dict:
    if model.startswith("gemini-"):
        return _call_gemini(
            model,
            messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
    if model.startswith("claude-"):
        return _call_anthropic(
            model,
            messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
    raise LLMCallError(f"unsupported model prefix: {model}")


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((LLMCallError,)),
    reraise=True,
)
def _dispatch_with_retry(
    model: str,
    messages: list[dict],
    *,
    temperature: float,
    max_output_tokens: int,
) -> dict:
    return _dispatch(
        model,
        messages,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


def call(
    model: str,
    messages: list[dict],
    *,
    response_schema: dict | None = None,
    cache_key: str | None = None,
    fallback_model: str | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 2048,
) -> dict:
    """Unified LLM call. Returns a dict with keys:

    ``text``, ``raw``, ``model``, ``prompt_tokens``, ``completion_tokens``,
    ``_cached``.
    """
    prompt_hash = _hash_prompt(model, messages, response_schema, cache_key)

    with _db_lock:
        conn = _connect_cache()
        try:
            cached = _cache_get(conn, prompt_hash)
        finally:
            conn.close()

    if cached is not None:
        cached = dict(cached)
        cached["_cached"] = True
        return cached

    try:
        result = _dispatch_with_retry(
            model,
            messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
    except RateLimitError:
        if fallback_model is None:
            raise
        logger.warning("rate-limited on %s; falling back to %s", model, fallback_model)
        result = _dispatch_with_retry(
            fallback_model,
            messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        # Cache under the original prompt hash so repeated hits are cheap,
        # but record the fallback model in the payload.
        result["fallback_from"] = model

    result["_cached"] = False

    with _db_lock:
        conn = _connect_cache()
        try:
            _cache_put(conn, model, prompt_hash, {k: v for k, v in result.items() if k != "_cached"})
        finally:
            conn.close()

    return result
