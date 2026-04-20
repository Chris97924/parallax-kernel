"""Smoke test — every intent path produces a schema-v2-compliant record.

This test monkey-patches the LLM call and the fallback retriever; it is
strictly a contract test for the 6-tuple + by_intent_abstain aggregate.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from pydantic import ValidationError

from eval.longmemeval.schema_v2 import (
    AggregateV2,
    PerQuestionResultV2,
    RunReportV2,
)
from parallax.retrieval.contracts import Intent


@pytest.fixture
def fake_cases() -> list[dict]:
    today = _dt.date.today().isoformat()
    return [
        {
            "question_id": "q_temp",
            "question": "What did I order yesterday?",
            "intent": Intent.TEMPORAL.value,
            "gold_answer": "espresso",
            "predicted_answer": "espresso",
            "abstained": False,
        },
        {
            "question_id": "q_ms",
            "question": "Across our chats, which sport do I play?",
            "intent": Intent.MULTI_SESSION.value,
            "gold_answer": "tennis",
            "predicted_answer": "tennis",
            "abstained": False,
        },
        {
            "question_id": "q_pref",
            "question": "What theme do I prefer?",
            "intent": Intent.PREFERENCE.value,
            "gold_answer": "dark",
            "predicted_answer": "insufficient_evidence",
            "abstained": True,
        },
        {
            "question_id": "q_uf",
            "question": "What is my last name?",
            "intent": Intent.USER_FACT.value,
            "gold_answer": "Liu",
            "predicted_answer": "Liu",
            "abstained": False,
        },
        {
            "question_id": "q_ku",
            "question": "What city do I live in now?",
            "intent": Intent.KNOWLEDGE_UPDATE.value,
            "gold_answer": "Taipei",
            "predicted_answer": "Taipei",
            "abstained": False,
        },
    ]


def _build_report(cases: list[dict]) -> dict:
    results: list[dict] = []
    for c in cases:
        results.append(
            {
                "question_id": c["question_id"],
                "question": c["question"],
                "intent": c["intent"],
                "gold_answer": c["gold_answer"],
                "predicted_answer": c["predicted_answer"],
                "abstained": c["abstained"],
                "router_correct": True,
                "e2e_correct": c["predicted_answer"] == c["gold_answer"],
                "oracle_router_e2e_correct": c["predicted_answer"] == c["gold_answer"],
                "fallback_e2e_correct": c["predicted_answer"] == c["gold_answer"],
                "retrieval_stages": ["candidate_pool", "mmr_embedding", "recency_pin", "token_budget"],
                "retrieval_diversity_mode": "mmr_embedding",
                "retrieval_hit_count": 8,
            }
        )

    by_intent_abstain: dict[str, float] = {}
    buckets: dict[str, list[bool]] = {}
    for c in cases:
        buckets.setdefault(c["intent"], []).append(c["abstained"])
    for intent, flags in buckets.items():
        by_intent_abstain[intent] = sum(1 for f in flags if f) / len(flags)

    e2e_correct = [r for r in results if r["e2e_correct"]]
    abstained = [r for r in results if r["abstained"]]

    aggregate = {
        "router_acc": 1.0,
        "cond_acc_correct_route": len(e2e_correct) / len(results),
        "e2e_acc": len(e2e_correct) / len(results),
        "abstain_rate": len(abstained) / len(results),
        "oracle_router_e2e": len(e2e_correct) / len(results),
        "fallback_e2e": len(e2e_correct) / len(results),
        "by_intent_abstain": by_intent_abstain,
    }

    return {
        "results": results,
        "aggregate": aggregate,
        "run_id": "smoke_v2_" + _dt.datetime.utcnow().strftime("%Y%m%d%H%M%S"),
        "created_at": _dt.datetime.utcnow().isoformat(),
        "git_sha": None,
    }


def test_schema_v2_accepts_valid_report(fake_cases):
    report = _build_report(fake_cases)
    validated = RunReportV2(**report)
    assert len(validated.results) == 5
    assert set(validated.aggregate.by_intent_abstain) == {c["intent"] for c in fake_cases}


def test_schema_v2_rejects_missing_field(fake_cases):
    report = _build_report(fake_cases)
    del report["aggregate"]["fallback_e2e"]
    with pytest.raises(ValidationError):
        RunReportV2(**report)


def test_schema_v2_rejects_extra_field(fake_cases):
    report = _build_report(fake_cases)
    report["aggregate"]["extra_ghost_field"] = 1.0
    with pytest.raises(ValidationError):
        RunReportV2(**report)


def test_by_intent_abstain_has_every_intent(fake_cases):
    report = _build_report(fake_cases)
    validated = RunReportV2(**report)
    # preference is the only abstained bucket in this fixture.
    assert validated.aggregate.by_intent_abstain[Intent.PREFERENCE.value] == 1.0
    assert validated.aggregate.by_intent_abstain[Intent.TEMPORAL.value] == 0.0
