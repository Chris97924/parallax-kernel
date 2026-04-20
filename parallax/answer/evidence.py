"""Evidence-only answerer.

Uses a **semantic** instruction so the model can perform reasonable logical
deduction (e.g. resolving a relative time reference), while still gating on
``insufficient_evidence`` when the corpus genuinely does not support the
question. The prompt deliberately avoids the phrase "exact quotes" — past
runs showed that wording drove over-abstention on paraphrased evidence.
"""

from __future__ import annotations

import datetime as _dt

from parallax.llm.call import call
from parallax.retrieval.contracts import (
    INSUFFICIENT_EVIDENCE,
    AnswerOutput,
    RetrievalEvidence,
)

SYSTEM_PROMPT_BASE = """\
Base your answer on the semantic meaning of the provided evidence.
Reasonable logical deduction is allowed (e.g. resolving relative time
references against today's date, combining two facts).
Only respond with `insufficient_evidence` when the evidence genuinely
does not support any reasonable inference toward the answer.
"""


def _render_evidence(evidence: RetrievalEvidence) -> str:
    lines: list[str] = []
    for i, hit in enumerate(evidence.hits, 1):
        text = hit.get("text", "")
        ts = hit.get("created_at", "unknown")
        lines.append(f"[{i}] {text} (at {ts})")
    if not lines:
        return "(no evidence)"
    return "\n".join(lines)


def _build_system(today: str) -> str:
    return SYSTEM_PROMPT_BASE + f"\nToday is {today}."


def answer(
    evidence: RetrievalEvidence,
    question: str,
    *,
    model: str = "gemini-2.5-pro",
    question_id: str | None = None,
    today: str | None = None,
    fallback_model: str | None = "gemini-2.5-flash",
) -> AnswerOutput:
    today = today or _dt.date.today().isoformat()
    system = _build_system(today)
    user = (
        "Evidence:\n"
        f"{_render_evidence(evidence)}\n\n"
        f"Question: {question}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    cache_key = f"answer::{question_id or question}::{len(evidence.hits)}"
    result = call(
        model,
        messages,
        cache_key=cache_key,
        fallback_model=fallback_model,
        temperature=0.0,
        max_output_tokens=512,
    )
    text = (result.get("text") or "").strip()
    low = text.lower()
    if low == INSUFFICIENT_EVIDENCE or low.startswith(INSUFFICIENT_EVIDENCE):
        return AnswerOutput(
            answer=INSUFFICIENT_EVIDENCE,
            abstained=True,
            model=result.get("model", model),
            prompt_tokens=result.get("prompt_tokens"),
            completion_tokens=result.get("completion_tokens"),
            notes=("abstained_insufficient_evidence",),
        )
    return AnswerOutput(
        answer=text,
        abstained=False,
        model=result.get("model", model),
        prompt_tokens=result.get("prompt_tokens"),
        completion_tokens=result.get("completion_tokens"),
    )
