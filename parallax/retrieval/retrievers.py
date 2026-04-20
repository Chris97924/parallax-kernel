"""Retrievers for ADR-006 Phase 1.

``fallback_retrieve`` is the safety net any intent-specific retriever can
demote to when its rules under-deliver. It uses semantic embeddings to pick
diverse candidates, pins the three most recent to the front (lost-in-middle
defense), and trims the tail to respect a hard token budget.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from parallax.retrieval.contracts import RetrievalEvidence

logger = logging.getLogger(__name__)

K_MAX_DEFAULT: int = 32
K_MIN_DEFAULT: int = 3
MAX_EVIDENCE_TOKENS: int = 6000

_MODEL: Any = None
_MODEL_LOAD_ERROR: Exception | None = None
_MODEL_LOCK = threading.Lock()

# Keyed by (user_id, max_created_at_in_candidate_pool). Value is the numpy
# array of item embeddings. Invalidated implicitly whenever a newer claim /
# event is ingested (the key changes). Sweeps that replay the same corpus
# repeatedly (ablate_fallback, sweep_thresholds) see a single encode cost.
_EMB_CACHE: dict[tuple[str, str], Any] = {}
_EMB_CACHE_LOCK = threading.Lock()


def _load_model() -> Any:
    global _MODEL, _MODEL_LOAD_ERROR
    # Fast path: read without acquiring the lock. _MODEL / _MODEL_LOAD_ERROR
    # writes below happen inside the lock, so a reader that sees either
    # non-None sees a fully initialised value.
    if _MODEL is not None:
        return _MODEL
    if _MODEL_LOAD_ERROR is not None:
        return None
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        if _MODEL_LOAD_ERROR is not None:
            return None
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            _MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        except Exception as exc:  # pragma: no cover
            logger.warning("sentence-transformers unavailable: %s", exc)
            _MODEL_LOAD_ERROR = exc
            _MODEL = None
    return _MODEL


def _embed_items_cached(model: Any, user_id: str, candidates: list[dict]) -> Any:
    """Return item embeddings, cached by (user_id, max created_at)."""
    max_ts = max((c.get("created_at") or "" for c in candidates), default="")
    key = (user_id, max_ts)
    with _EMB_CACHE_LOCK:
        hit = _EMB_CACHE.get(key)
        if hit is not None and len(hit) == len(candidates):
            return hit
    texts = [c["text"] for c in candidates]
    embs = model.encode(texts, convert_to_numpy=True)
    with _EMB_CACHE_LOCK:
        _EMB_CACHE[key] = embs
    return embs


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _fetch_candidates(
    conn: sqlite3.Connection,
    user_id: str,
    pool_size: int,
) -> list[dict]:
    """Pull the most recent claims and events for ``user_id``.

    Returns a flat list of dicts: {id, source_id, text, created_at, kind}.
    """
    rows: list[dict] = []
    cur = conn.execute(
        """
        SELECT claim_id, user_id, subject, predicate, object, source_id,
               created_at, 'claim' AS kind
        FROM claims
        WHERE user_id = ? AND state != 'deleted'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, pool_size),
    )
    for row in cur.fetchall():
        rows.append(
            {
                "id": row[0],
                "source_id": row[5],
                "text": f"{row[2]} {row[3]} {row[4]}".strip(),
                "created_at": row[6],
                "kind": "claim",
            }
        )

    cur = conn.execute(
        """
        SELECT event_id, user_id, event_type, target_id, payload_json, created_at
        FROM events
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, pool_size),
    )
    for row in cur.fetchall():
        payload = row[4] or "{}"
        try:
            parsed = json.loads(payload)
            text_blob = (
                parsed.get("text")
                or parsed.get("content")
                or parsed.get("summary")
                or json.dumps(parsed, ensure_ascii=False)
            )
        except (json.JSONDecodeError, TypeError):
            text_blob = payload
        rows.append(
            {
                "id": row[0],
                "source_id": row[3],
                "text": f"{row[2]}: {text_blob}".strip(),
                "created_at": row[5],
                "kind": "event",
            }
        )
    return rows


def _bm25_stub_rank(query: str, items: list[dict], k: int) -> list[tuple[int, float]]:
    """Lexical-overlap fallback when embeddings are unavailable."""
    q_tokens = {t.lower() for t in query.split() if t}
    scored: list[tuple[int, float]] = []
    for i, item in enumerate(items):
        tokens = {t.lower() for t in item["text"].split() if t}
        overlap = len(q_tokens & tokens) / max(1, len(q_tokens))
        scored.append((i, overlap))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]


def _mmr_rank(
    query_emb: Any,
    item_embs: Any,
    k: int,
    lambda_: float = 0.7,
) -> list[int]:
    import numpy as np  # local import — numpy ships with sentence-transformers

    def _cos(a, b):
        na = np.linalg.norm(a)
        nb = np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    n = len(item_embs)
    remaining = list(range(n))
    selected: list[int] = []
    sims_to_query = [_cos(query_emb, item_embs[i]) for i in range(n)]

    while remaining and len(selected) < k:
        best_idx = remaining[0]
        best_score = -1e9
        for idx in remaining:
            relevance = sims_to_query[idx]
            if selected:
                redundancy = max(_cos(item_embs[idx], item_embs[s]) for s in selected)
            else:
                redundancy = 0.0
            mmr = lambda_ * relevance - (1 - lambda_) * redundancy
            if mmr > best_score:
                best_score = mmr
                best_idx = idx
        selected.append(best_idx)
        remaining.remove(best_idx)
    return selected


def fallback_retrieve(
    conn: sqlite3.Connection,
    user_id: str,
    query: str,
    *,
    k_max: int = K_MAX_DEFAULT,
    k_min: int = K_MIN_DEFAULT,
    max_evidence_tokens: int = MAX_EVIDENCE_TOKENS,
) -> RetrievalEvidence:
    """Semantic-diverse fallback retriever. Always returns ``RetrievalEvidence``."""
    pool_size = max(200, k_max * 8)
    candidates = _fetch_candidates(conn, user_id, pool_size)
    stages: list[str] = ["candidate_pool"]
    notes: list[str] = []
    sql_fragments: tuple[str, ...] = (
        "SELECT ... FROM claims WHERE user_id = ? AND state != 'deleted' ORDER BY created_at DESC",
        "SELECT ... FROM events WHERE user_id = ? ORDER BY created_at DESC",
    )

    if not candidates:
        return RetrievalEvidence(
            hits=(),
            stages=tuple(stages),
            notes=("empty_candidate_pool", "demoted_to_fallback"),
            sql_fragments=sql_fragments,
            diversity_mode="none",
        )

    model = _load_model()
    if model is None:
        diversity_mode = "mmr_stub_bm25"
        stages.append(diversity_mode)
        ranked = _bm25_stub_rank(query, candidates, k_max)
        selected_idx = [i for i, _ in ranked]
    else:
        diversity_mode = "mmr_embedding"
        stages.append(diversity_mode)
        q_emb = model.encode([query], convert_to_numpy=True)[0]
        item_embs = _embed_items_cached(model, user_id, candidates)
        selected_idx = _mmr_rank(q_emb, item_embs, k_max)

    selected = [candidates[i] for i in selected_idx]

    # Recency pin: top-3 by created_at desc, moved to front.
    stages.append("recency_pin")
    recency_sorted = sorted(
        range(len(selected)),
        key=lambda i: selected[i]["created_at"] or "",
        reverse=True,
    )
    top3 = recency_sorted[:3]
    front = [selected[i] for i in top3]
    rest = [selected[i] for i in range(len(selected)) if i not in top3]
    ordered = front + rest

    # Token budget: drop tail items whose inclusion would exceed the ceiling.
    stages.append("token_budget")
    running = 0
    kept: list[dict] = []
    for item in ordered:
        t = _estimate_tokens(item["text"])
        if running + t > max_evidence_tokens and kept:
            break
        kept.append(item)
        running += t

    if len(kept) < k_min:
        notes.append("demoted_to_fallback")

    return RetrievalEvidence(
        hits=tuple(kept),
        stages=tuple(stages),
        notes=tuple(notes),
        sql_fragments=sql_fragments,
        diversity_mode=diversity_mode,
    )
