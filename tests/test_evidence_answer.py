"""Tests for parallax.answer.evidence.answer — semantic prompt + abstain path."""

from __future__ import annotations

import parallax.answer.evidence as evidence_module
from parallax.answer.evidence import answer
from parallax.retrieval.contracts import INSUFFICIENT_EVIDENCE, RetrievalEvidence


def _evidence(hits: list[dict] | None = None) -> RetrievalEvidence:
    return RetrievalEvidence(
        hits=tuple(hits or [{"id": "c1", "text": "Chris prefers dark mode.", "created_at": "2026-04-01", "source_id": "s1", "kind": "claim"}]),
        stages=("mmr_embedding",),
        diversity_mode="mmr_embedding",
    )


def test_abstain_path(monkeypatch):
    captured: dict = {}

    def fake_call(model, messages, **kw):
        captured["model"] = model
        captured["messages"] = messages
        return {
            "text": "insufficient_evidence",
            "model": model,
            "prompt_tokens": 10,
            "completion_tokens": 2,
        }

    monkeypatch.setattr(evidence_module, "call", fake_call)

    out = answer(_evidence([]), "What is Chris' favourite theme?")
    assert out.abstained is True
    assert out.answer == INSUFFICIENT_EVIDENCE


def test_semantic_answer_path(monkeypatch):
    def fake_call(model, messages, **kw):
        return {
            "text": "Chris prefers dark mode.",
            "model": model,
            "prompt_tokens": 20,
            "completion_tokens": 5,
        }

    monkeypatch.setattr(evidence_module, "call", fake_call)

    out = answer(_evidence(), "What theme does Chris prefer?")
    assert out.abstained is False
    assert "dark mode" in out.answer.lower()


def test_prompt_is_semantic_not_exact_quotes(monkeypatch):
    seen: dict = {}

    def fake_call(model, messages, **kw):
        seen["messages"] = messages
        return {"text": "ok", "model": model, "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(evidence_module, "call", fake_call)

    answer(_evidence(), "Q?")
    system = seen["messages"][0]["content"]
    assert seen["messages"][0]["role"] == "system"
    assert "semantic meaning" in system
    assert "exact quotes" not in system.lower()
    assert "insufficient_evidence" in system
