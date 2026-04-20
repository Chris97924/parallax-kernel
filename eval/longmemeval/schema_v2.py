"""ADR-006 schema v2 for LongMemEval runs (pydantic)."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from pydantic import BaseModel, ConfigDict


class PerQuestionResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    question: str
    intent: str
    gold_answer: str
    predicted_answer: str
    abstained: bool
    router_correct: bool
    e2e_correct: bool
    oracle_router_e2e_correct: bool
    fallback_e2e_correct: bool
    retrieval_stages: list[str]
    retrieval_diversity_mode: str
    retrieval_hit_count: int


class AggregateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    router_acc: float
    cond_acc_correct_route: float
    e2e_acc: float
    abstain_rate: float
    oracle_router_e2e: float
    fallback_e2e: float
    by_intent_abstain: dict[str, float]


class RunReportV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[PerQuestionResultV2]
    aggregate: AggregateV2
    run_id: str
    created_at: str
    git_sha: str | None = None


def write_run_report_v2(path: pathlib.Path | str, report: dict[str, Any]) -> None:
    """Validate ``report`` through :class:`RunReportV2` then write JSON."""
    validated = RunReportV2(**report)
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(validated.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
