"""Contract tests for ``eval.longmemeval.store.build_from_parallax_retrieval``.

Closes the v0.6 LongMemEval harness bug: the v1 pipeline built the answer
prompt from ``dump_all_sessions(q)`` which walked the in-memory Question
tuple, bypassing the Parallax store. This module asserts the new helper
actually reads back through the store we ingested into.

No LLM calls — everything is deterministic SQLite round-trip.
"""

from __future__ import annotations

from eval.longmemeval.dataset import Question, Session, Turn
from eval.longmemeval.store import (
    build_from_parallax_retrieval,
    ephemeral_store,
    ingest_question,
)


def _fixture_question() -> Question:
    """Minimal Question with two sessions covering a clear topical split."""
    sessions = (
        Session(
            session_id="s1",
            date="2026-01-01",
            turns=(
                Turn(role="user", content="My favourite colour is teal.", has_answer=True),
                Turn(role="assistant", content="Teal noted.", has_answer=False),
            ),
        ),
        Session(
            session_id="s2",
            date="2026-01-02",
            turns=(
                Turn(role="user", content="I just adopted a tabby cat named Mochi.", has_answer=False),
                Turn(role="assistant", content="Congrats on Mochi!", has_answer=False),
            ),
        ),
    )
    return Question(
        question_id="q_retrieval_smoke",
        question_type="single-session-user",
        question="What colour did I say I liked?",
        answer="teal",
        question_date="2026-02-01",
        sessions=sessions,
        answer_session_ids=("s1",),
    )


def test_build_from_parallax_retrieval_reads_through_store() -> None:
    """Transcript must reflect data read back from the Parallax conn, not q."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q)

    assert transcript, "expected non-empty transcript from Parallax store"
    # The gold content lives in the user turn of session s1.
    assert "teal" in transcript.lower()


def test_build_from_parallax_retrieval_empty_store_returns_empty_string() -> None:
    """A fresh store with nothing ingested must return '' (not raise)."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        transcript = build_from_parallax_retrieval(conn, q)
    assert transcript == ""


def test_build_from_parallax_retrieval_respects_top_k() -> None:
    """top_k=1 must keep at most one memory row in the transcript."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q, top_k=1)
    # 4 turns were ingested; top_k=1 keeps only the most relevant one.
    # Output format is "title\nsummary", rows separated by "\n\n".
    blocks = [b for b in transcript.split("\n\n") if b.strip()]
    assert len(blocks) <= 1


def test_build_from_parallax_retrieval_respects_char_budget() -> None:
    """A tight max_chars cap must stop emission after the first block."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q, max_chars=10)
    # With max_chars=10, only the first block is kept (the guard allows one
    # block to exceed the cap so the output is never silently empty).
    blocks = [b for b in transcript.split("\n\n") if b.strip()]
    assert len(blocks) == 1


def test_build_from_parallax_retrieval_chronological_order() -> None:
    """Kept rows must emerge in vault_path order (not relevance order)."""
    q = _fixture_question()
    with ephemeral_store() as conn:
        ingest_question(conn, q)
        transcript = build_from_parallax_retrieval(conn, q)

    # vault_path is "lme/{qid}/s{si}/t{ti}"; earlier session must appear first.
    s1_idx = transcript.find("Teal noted")
    s2_idx = transcript.find("Mochi")
    assert s1_idx != -1 and s2_idx != -1, "expected both sessions in output"
    assert s1_idx < s2_idx, "chronological order (s1 before s2) must be preserved"
