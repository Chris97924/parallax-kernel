"""End-to-end per-question pipeline: ingest → retrieve → answer → judge.

v1 strategy:

* Ingest every turn into a fresh Parallax store.
* For the answer step, dump the full session transcript into Gemini
  3.x Pro's 1M-context window. No retrieval filter yet — we establish
  a long-context ceiling first.
* Judge with the same model family using the LongMemEval-style
  ``correct / incorrect`` rubric: the judge is asked to decide whether
  the prediction communicates the gold answer, tolerating paraphrase.

A later v2 can swap :func:`build_answer_prompt` for a retrieval-filtered
context without touching :func:`judge` or the runner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from eval.longmemeval.dataset import Question
from eval.longmemeval.gemini import GeminiResult, call
from eval.longmemeval.store import (
    build_from_parallax_retrieval,
    dump_all_sessions,
    ephemeral_store,
    ingest_question,
)

logger = logging.getLogger(__name__)


ANSWER_SYSTEM = (
    "You are a careful assistant answering a user's question about their "
    "past conversations. Use ONLY the provided chat history to answer. "
    "If the answer is not present, say 'I don't know'. Keep the answer "
    "concise — typically one short sentence or a single phrase."
)

JUDGE_SYSTEM = (
    "You are a strict grading assistant. Given a QUESTION, a GOLD ANSWER, "
    "and a PREDICTION, decide whether the prediction correctly answers "
    "the question. Paraphrases and different wordings that communicate "
    "the same fact count as CORRECT. Missing information or wrong facts "
    "count as INCORRECT. Output exactly one token: CORRECT or INCORRECT, "
    "then on a new line a brief reason."
)


@dataclass(frozen=True)
class AnswerRecord:
    question_id: str
    question_type: str
    question: str
    gold: str
    prediction: str
    verdict: str  # CORRECT | INCORRECT | ERROR
    judge_reason: str
    turns_ingested: int
    answer_prompt_tokens: int
    answer_output_tokens: int
    judge_prompt_tokens: int
    judge_output_tokens: int
    answer_model: str
    judge_model: str


def build_answer_prompt(q: Question, transcript: str) -> str:
    return (
        f"CURRENT DATE: {q.question_date}\n\n"
        f"CHAT HISTORY (chronological, user-plus-assistant turns):\n\n"
        f"{transcript}\n\n"
        f"---\n"
        f"QUESTION: {q.question}\n\n"
        f"Answer the question based strictly on the chat history above. "
        f"Respond with a short, direct answer only — no preamble."
    )


def build_judge_prompt(q: Question, prediction: str) -> str:
    return (
        f"QUESTION: {q.question}\n\n"
        f"GOLD ANSWER: {q.answer}\n\n"
        f"PREDICTION: {prediction}\n\n"
        f"Is the PREDICTION correct? Reply on the first line with exactly "
        f"CORRECT or INCORRECT, then a one-line reason."
    )


def parse_verdict(judge_text: str) -> tuple[str, str]:
    first, _, rest = judge_text.strip().partition("\n")
    head = first.strip().upper().split()
    verdict = "INCORRECT"
    if head and head[0] in {"CORRECT", "INCORRECT"}:
        verdict = head[0]
    return verdict, rest.strip()


def run_one(
    q: Question,
    *,
    answer_model: str,
    judge_model: str,
    max_output_tokens: int = 512,
    use_retrieval: bool = False,
) -> AnswerRecord:
    """Run the full pipeline for a single question. Exceptions become ERROR.

    When ``use_retrieval`` is False (default, preserves v1 long-context
    behavior), the answer prompt is built from ``dump_all_sessions(q)`` —
    walking the Question tuple directly. When True, the prompt is built by
    ``build_from_parallax_retrieval(conn, q)``, which reads back through the
    Parallax store we just ingested into. The retrieval path is opt-in so
    existing Run B baselines stay reproducible.
    """
    try:
        with ephemeral_store() as conn:
            turns = ingest_question(conn, q)
            transcript = (
                build_from_parallax_retrieval(conn, q)
                if use_retrieval
                else dump_all_sessions(q)
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ingest failed for %s", q.question_id)
        return _err_record(q, answer_model, judge_model, str(exc), stage="ingest")

    # Fail loud when retrieval returns nothing. Passing an empty transcript
    # to the answer model would produce a plausible hallucinated prediction
    # with no signal that the context was missing — silent benchmark poison.
    if use_retrieval and not transcript:
        logger.warning(
            "build_from_parallax_retrieval returned empty transcript for %s",
            q.question_id,
        )
        return _err_record(
            q,
            answer_model,
            judge_model,
            "retrieval returned empty transcript",
            stage="retrieval",
        )

    try:
        ans: GeminiResult = call(
            model=answer_model,
            user=build_answer_prompt(q, transcript),
            system=ANSWER_SYSTEM,
            max_output_tokens=max_output_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("answer failed for %s", q.question_id)
        return _err_record(q, answer_model, judge_model, str(exc), stage="answer")

    try:
        jr: GeminiResult = call(
            model=judge_model,
            user=build_judge_prompt(q, ans.text),
            system=JUDGE_SYSTEM,
            max_output_tokens=256,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("judge failed for %s", q.question_id)
        return AnswerRecord(
            question_id=q.question_id,
            question_type=q.question_type,
            question=q.question,
            gold=q.answer,
            prediction=ans.text,
            verdict="ERROR",
            judge_reason=f"judge exception: {exc}",
            turns_ingested=turns,
            answer_prompt_tokens=ans.prompt_tokens,
            answer_output_tokens=ans.output_tokens,
            judge_prompt_tokens=0,
            judge_output_tokens=0,
            answer_model=answer_model,
            judge_model=judge_model,
        )

    verdict, reason = parse_verdict(jr.text)
    return AnswerRecord(
        question_id=q.question_id,
        question_type=q.question_type,
        question=q.question,
        gold=q.answer,
        prediction=ans.text,
        verdict=verdict,
        judge_reason=reason,
        turns_ingested=turns,
        answer_prompt_tokens=ans.prompt_tokens,
        answer_output_tokens=ans.output_tokens,
        judge_prompt_tokens=jr.prompt_tokens,
        judge_output_tokens=jr.output_tokens,
        answer_model=answer_model,
        judge_model=judge_model,
    )


def _err_record(
    q: Question, answer_model: str, judge_model: str, msg: str, *, stage: str
) -> AnswerRecord:
    return AnswerRecord(
        question_id=q.question_id,
        question_type=q.question_type,
        question=q.question,
        gold=q.answer,
        prediction="",
        verdict="ERROR",
        judge_reason=f"{stage} exception: {msg[:240]}",
        turns_ingested=0,
        answer_prompt_tokens=0,
        answer_output_tokens=0,
        judge_prompt_tokens=0,
        judge_output_tokens=0,
        answer_model=answer_model,
        judge_model=judge_model,
    )
