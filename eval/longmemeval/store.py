"""Per-question Parallax store helpers.

LongMemEval protocol resets memory between questions: each question carries
its own haystack, so we spin up an isolated SQLite file per question, ingest
every turn as a :class:`parallax.Memory`, and tear down at the end.

This v1 uses Parallax as a session-indexed memory store; no LLM claim
extraction is performed. Retrieval falls back to the full memory dump,
which a 1M-ctx answer model can consume directly. A v2 adapter can layer
:func:`parallax.extract.extract_and_ingest` on top for claim-level
retrieval without changing this surface.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

from parallax import ingest_memory, memories_by_user, migrate_to_latest
from parallax.sqlite_store import connect

from eval.longmemeval.dataset import Question


@contextmanager
def ephemeral_store() -> Iterator[sqlite3.Connection]:
    """Yield a fresh migrated Parallax DB that is deleted on exit."""
    with TemporaryDirectory(prefix="parallax_lme_") as tmpdir:
        db_path = Path(tmpdir) / "parallax.db"
        conn = connect(str(db_path))
        try:
            migrate_to_latest(conn)
            yield conn
        finally:
            conn.close()


def ingest_question(conn: sqlite3.Connection, q: Question) -> int:
    """Ingest every turn of a Question as a memory row. Returns turn count."""
    user_id = q.question_id
    n = 0
    for si, sess in enumerate(q.sessions):
        for ti, turn in enumerate(sess.turns):
            vault_path = f"lme/{q.question_id}/s{si}/t{ti}"
            title = f"[{sess.date}] {turn.role}"
            ingest_memory(
                conn,
                user_id=user_id,
                title=title,
                summary=turn.content,
                vault_path=vault_path,
            )
            n += 1
    return n


def dump_all_sessions(q: Question) -> str:
    """Render the full haystack as a single chronological transcript.

    Used by the v1 long-context answer strategy. Sessions are emitted in
    their listed order (not sorted by date — the dataset preserves the
    user's original arrival order, which matters for temporal questions).
    """
    blocks: list[str] = []
    for si, sess in enumerate(q.sessions):
        header = f"### Session {si + 1} — {sess.date} (id={sess.session_id})"
        lines = [header]
        for turn in sess.turns:
            lines.append(f"{turn.role.upper()}: {turn.content}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def dump_via_memories(conn: sqlite3.Connection, user_id: str) -> str:
    """Alternative: reconstruct transcript from Parallax memory rows.

    Proves the ingest round-trip is lossless — useful for smoke assertions.
    """
    rows = memories_by_user(conn, user_id)
    rows_sorted = sorted(rows, key=lambda r: r["vault_path"])
    return "\n".join(f"{r['title']}\n{r['summary']}" for r in rows_sorted)


def build_from_parallax_retrieval(
    conn: sqlite3.Connection,
    q: Question,
    *,
    top_k: int = 64,
    max_chars: int = 40000,
) -> str:
    """Retrieval-filtered transcript — reads from Parallax store, not from ``q``.

    The v1 pipeline built the answer prompt from ``dump_all_sessions(q)``,
    which walks the in-memory Question tuple and therefore never exercises the
    Parallax store we just ingested into. That bypass hid any breakage in the
    ingest/retrieve round-trip behind a 1M-ctx answer model.

    This helper closes the loop:

    1. Fetch every ingested memory row via ``memories_by_user(conn, ...)``.
    2. Score each row by lexical overlap with ``q.question`` (BM25-stub — no
       model dependency, deterministic, fast).
    3. Keep the top ``top_k`` rows, stop early once ``max_chars`` is reached.
    4. Emit a chronological transcript ordered by ``vault_path`` so temporal
       questions keep the user's original arrival order.

    The defaults are generous (64 rows / 40K chars) so short haystacks pass
    through unfiltered; narrow them to actually exercise retrieval pressure.
    """
    user_id = q.question_id
    rows = memories_by_user(conn, user_id)
    if not rows:
        return ""

    q_tokens = {t.lower() for t in q.question.split() if t}

    def _score(row: dict) -> float:
        blob = f"{row.get('title') or ''} {row.get('summary') or ''}"
        tokens = {t.lower() for t in blob.split() if t}
        return len(q_tokens & tokens) / max(1, len(q_tokens))

    scored: list[tuple[float, dict]] = [(_score(r), r) for r in rows]
    scored.sort(key=lambda item: item[0], reverse=True)
    kept = [r for _, r in scored[:top_k]]

    # Restore chronological order for the emitted transcript — relevance is used
    # only to choose which rows survive the top_k + char budget.
    kept.sort(key=lambda r: r.get("vault_path") or "")

    lines: list[str] = []
    total = 0
    for r in kept:
        block = f"{r['title']}\n{r['summary']}"
        if total + len(block) > max_chars and lines:
            break
        lines.append(block)
        total += len(block)
    return "\n\n".join(lines)
