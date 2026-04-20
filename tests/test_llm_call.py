"""Tests for parallax.llm.call — cache behavior and fallback model."""

from __future__ import annotations

import pytest

import parallax.llm.call as call_module
from parallax.llm.call import RateLimitError, call


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PARALLAX_LLM_CACHE", str(tmp_path / "cache.sqlite"))
    yield


def test_cache_miss_then_hit(isolated_cache, monkeypatch):
    calls: list[tuple] = []

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        calls.append((model, tuple(m["content"] for m in messages)))
        return {
            "text": "hello",
            "raw": {},
            "model": model,
            "prompt_tokens": 3,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    msgs = [{"role": "user", "content": "hi"}]
    first = call("gemini-2.5-flash", msgs)
    assert first["_cached"] is False
    assert first["text"] == "hello"

    second = call("gemini-2.5-flash", msgs)
    assert second["_cached"] is True
    assert second["text"] == "hello"
    assert len(calls) == 1, "dispatcher should have been called exactly once"


def test_cache_key_override(isolated_cache, monkeypatch):
    call_count = {"n": 0}

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        call_count["n"] += 1
        return {
            "text": "same-key",
            "raw": {},
            "model": model,
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    a = call("gemini-2.5-flash", [{"role": "user", "content": "A"}], cache_key="k1")
    b = call("gemini-2.5-flash", [{"role": "user", "content": "B"}], cache_key="k1")
    assert a["_cached"] is False
    assert b["_cached"] is True
    assert call_count["n"] == 1


def test_fallback_on_rate_limit(isolated_cache, monkeypatch):
    call_count = {"primary": 0, "fallback": 0}

    def fake_dispatch(model, messages, *, temperature, max_output_tokens):
        if model == "gemini-2.5-pro":
            call_count["primary"] += 1
            raise RateLimitError("429 simulated")
        call_count["fallback"] += 1
        return {
            "text": "ok",
            "raw": {},
            "model": model,
            "prompt_tokens": 2,
            "completion_tokens": 1,
        }

    monkeypatch.setattr(call_module, "_dispatch_with_retry", fake_dispatch)

    result = call(
        "gemini-2.5-pro",
        [{"role": "user", "content": "x"}],
        fallback_model="gemini-2.5-flash",
    )
    assert result["text"] == "ok"
    assert result["model"] == "gemini-2.5-flash"
    assert result["fallback_from"] == "gemini-2.5-pro"
    assert call_count["primary"] == 1
    assert call_count["fallback"] == 1
