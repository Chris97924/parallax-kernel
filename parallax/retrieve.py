"""Read-side helpers + v0.3.0 explicit retrieval API for Parallax.

Two surfaces coexist:

* **Legacy, row-level helpers** (``memories_by_user``, ``claims_by_user``,
  ``claims_by_subject``, ``memory_by_content_hash``, ``claim_by_content_hash``) —
  thin wrappers over :func:`parallax.sqlite_store.query` returning plain
  ``dict``. These are used by existing tests and callers; preserved as-is.

* **v0.3.0 explicit retrieval API** — six functions that replace the single
  vague ``search()`` entrypoint that older skills used. Each returns
  ``list[RetrievalHit]`` ordered by score descending, and every hit carries a
  ``project(level: int)`` method implementing 3-layer progressive disclosure
  (L1 title+score, L2 +evidence, L3 full row).

  * :func:`recent_context` — latest events for a session (or most recent session)
  * :func:`by_file` — events that touched a given file path
  * :func:`by_decision` — claim state-changes and decision events
  * :func:`by_bug_fix` — events/claims matching fix/bug tokens
  * :func:`by_timeline` — events in a timestamp window
  * :func:`by_entity` — claims + events referencing a subject

The score signal is deliberately simple (keyword/recency/source-weight mix) —
rich vector scoring is out of scope for v0.3.0.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import sqlite3

from parallax.obs.metrics import get_counter
from parallax.sqlite_store import query

_c_retrieve = get_counter("retrieve_total")

__all__ = [
    # Legacy helpers
    "memories_by_user",
    "claims_by_user",
    "claims_by_subject",
    "memory_by_content_hash",
    "claim_by_content_hash",
    # v0.3.0
    "RetrievalHit",
    "recent_context",
    "by_file",
    "by_decision",
    "by_bug_fix",
    "by_timeline",
    "by_entity",
    "FILE_EVENT_TYPES",
]



def _to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ----- Legacy helpers -------------------------------------------------------


def memories_by_user(
    conn: sqlite3.Connection, user_id: str, state: str | None = None
) -> list[dict]:
    _c_retrieve.inc()
    if state is None:
        rows = query(conn, "SELECT * FROM memories WHERE user_id = ?", (user_id,))
    else:
        rows = query(
            conn,
            "SELECT * FROM memories WHERE user_id = ? AND state = ?",
            (user_id, state),
        )
    return _to_dicts(rows)


def claims_by_user(
    conn: sqlite3.Connection, user_id: str, state: str | None = None
) -> list[dict]:
    _c_retrieve.inc()
    if state is None:
        rows = query(conn, "SELECT * FROM claims WHERE user_id = ?", (user_id,))
    else:
        rows = query(
            conn,
            "SELECT * FROM claims WHERE user_id = ? AND state = ?",
            (user_id, state),
        )
    return _to_dicts(rows)


def claims_by_subject(
    conn: sqlite3.Connection, user_id: str, subject: str
) -> list[dict]:
    rows = query(
        conn,
        "SELECT * FROM claims WHERE user_id = ? AND subject = ?",
        (user_id, subject),
    )
    return _to_dicts(rows)


def memory_by_content_hash(
    conn: sqlite3.Connection, content_hash: str
) -> dict | None:
    rows = query(
        conn, "SELECT * FROM memories WHERE content_hash = ? LIMIT 1", (content_hash,)
    )
    return dict(rows[0]) if rows else None


def claim_by_content_hash(
    conn: sqlite3.Connection, content_hash: str
) -> dict | None:
    """Lookup a claim by its pre-computed content_hash.

    Post-ADR-005 (v0.5.0-pre1) the hash is user-scoped:
    ``sha256(normalize(subject || predicate || object || source_id || user_id))``.
    Callers constructing the hash externally must pass all five parts; a
    4-part hash built with the pre-v0.5.0-pre1 formula will silently miss.
    """
    rows = query(
        conn, "SELECT * FROM claims WHERE content_hash = ? LIMIT 1", (content_hash,)
    )
    return dict(rows[0]) if rows else None


# ----- v0.3.0 RetrievalHit --------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RetrievalHit:
    """A single retrieval result with 3-layer progressive disclosure.

    Construct with ``entity_kind`` / ``entity_id`` / ``title`` / ``score`` /
    ``evidence`` (optional L2 one-sentence reason) / ``full`` (optional L3
    dict snapshot of the underlying row) / ``explain`` (mandatory audit
    metadata).

    ``explain`` always carries at minimum ``{'reason': str,
    'score_components': dict[str, float]}`` so score attribution is auditable.
    """

    entity_kind: str
    entity_id: str
    title: str
    score: float
    evidence: str | None
    full: dict | None
    explain: dict

    def project(self, level: int) -> dict:
        """Return the L1/L2/L3 projection. Raises ValueError on bad level."""
        if level not in (1, 2, 3):
            raise ValueError(f"level must be one of 1, 2, 3; got {level!r}")
        base = {
            "entity_kind": self.entity_kind,
            "entity_id": self.entity_id,
            "title": self.title,
            "score": self.score,
        }
        if level == 1:
            return base
        base["evidence"] = self.evidence
        if level == 2:
            return base
        # L3: include full row; fall back to evidence-only when full is None
        base["full"] = self.full if self.full is not None else self.evidence
        return base


# ----- v0.3.0 retrieval functions ------------------------------------------


_FIX_TOKENS = ("fix", "bug", "FIX-", "regression", "hotfix")

# Event types that represent file-editing activity. Shared with
# parallax.injector so both surfaces agree on what "a file edit" is.
FILE_EVENT_TYPES = ("tool.edit", "tool.write", "file.edit")

# Used with every LIKE clause that interpolates user-controlled text so that
# literal '%' and '_' in paths/subjects don't silently become wildcards.
_LIKE_ESCAPE = "\\"


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards in user-controlled text.

    Pair with ``ESCAPE '\\'`` in the SQL LIKE clause so that a path like
    ``utils_v2.py`` matches literally instead of treating ``_`` as a wildcard.
    """
    return (
        s.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _parse_iso(ts: str) -> _dt.datetime:
    """Parse ISO-8601; tolerant of trailing 'Z'."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(ts)


def _iso_normalize(ts: str, *, kind: str) -> str:
    """Normalize an ISO-8601 string to the exact form used by ``now_iso()``.

    The output always carries both a microsecond component and a ``+00:00``
    offset so lexicographic comparison against stored ``created_at`` values
    is stable. Required by the SQLite ``created_at >= ? AND created_at <= ?``
    window in :func:`by_timeline`:

    * ``kind='since'`` — micro=0 is preserved; bound is inclusive from the
      start of the second.
    * ``kind='until'`` — if the input microsecond is 0, it is expanded to
      ``999999`` so the second-boundary is inclusive to the end of the
      second. Without this, ``now_iso()`` rows like
      ``"...T12:00:00.500000+00:00"`` fall OUTSIDE a query whose
      ``until="...T12:00:00Z"`` normalized naively (BUG 1).

    ``kind`` is keyword-only and has no default: a silent miscall such as
    ``_iso_normalize(until_str)`` would apply ``since`` semantics to an
    ``until`` bound and drop boundary events. Requiring an explicit kind
    makes that misuse impossible.
    """
    if kind not in ("since", "until"):
        raise ValueError(f"kind must be 'since' or 'until'; got {kind!r}")
    dt = _parse_iso(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    dt = dt.astimezone(_dt.UTC)
    if kind == "until" and dt.microsecond == 0:
        dt = dt.replace(microsecond=999_999)
    # Force the ``.SSSSSS`` component so the lex-compared bound always has
    # the same layout as stored ``now_iso()`` rows (BUG 1/4).
    return dt.isoformat(timespec="microseconds")


def _recency_score(created_at: str, now: _dt.datetime | None = None) -> float:
    """Return a [0,1] recency weight; newer → higher."""
    try:
        t = _parse_iso(created_at)
    except Exception:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.UTC)
    ref = now or _dt.datetime.now(_dt.UTC)
    age_h = max((ref - t).total_seconds() / 3600.0, 0.0)
    # half-life ~ 24h
    return max(0.0, min(1.0, 1.0 / (1.0 + age_h / 24.0)))


def _event_title(row: dict) -> str:
    """Compact human-readable title for an event row."""
    etype = row.get("event_type", "event")
    tkind = row.get("target_kind")
    tid = row.get("target_id")
    if tkind and tid:
        return f"{etype} [{tkind}:{tid}]"
    return str(etype)


def _event_to_hit(row: dict, *, reason: str, score_components: dict[str, float]) -> RetrievalHit:
    score = round(sum(score_components.values()), 6)
    return RetrievalHit(
        entity_kind="event",
        entity_id=str(row.get("event_id", "")),
        title=_event_title(row),
        score=score,
        evidence=f"{row.get('created_at','?')} {row.get('event_type','?')} "
                 f"{row.get('payload_json','')[:160]}",
        full=row,
        explain={"reason": reason, "score_components": score_components},
    )


def _claim_to_hit(row: dict, *, reason: str, score_components: dict[str, float]) -> RetrievalHit:
    score = round(sum(score_components.values()), 6)
    title = f"{row.get('subject','')} {row.get('predicate','')} {row.get('object','')}"
    return RetrievalHit(
        entity_kind="claim",
        entity_id=str(row.get("claim_id", "")),
        title=title.strip() or "(empty claim)",
        score=score,
        evidence=f"confidence={row.get('confidence')} state={row.get('state')}",
        full=row,
        explain={"reason": reason, "score_components": score_components},
    )


def recent_context(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    session_id: str | None = None,
    limit: int = 20,
) -> list[RetrievalHit]:
    """Most recent events for a session.

    When ``session_id`` is None, the scan is scoped to the latest session (the
    session_id of the newest ``session.start`` row, if any; otherwise the
    newest event regardless of session). Score = recency (1/(1+age_h/24)).
    """
    if session_id is None:
        row = query(
            conn,
            "SELECT session_id FROM events WHERE user_id = ? AND event_type = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, "session.start"),
        )
        session_id = row[0]["session_id"] if row else None

    if session_id is None:
        rows = query(
            conn,
            "SELECT * FROM events WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        reason_tmpl = "recent_context fallback (no session.start found); newest events for user"
    else:
        rows = query(
            conn,
            "SELECT * FROM events WHERE user_id = ? AND session_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, session_id, limit),
        )
        reason_tmpl = f"recent_context match on session_id={session_id!r}"

    hits: list[RetrievalHit] = []
    for r in _to_dicts(rows):
        hits.append(
            _event_to_hit(
                r,
                reason=reason_tmpl,
                score_components={"recency": _recency_score(r.get("created_at", ""))},
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def by_file(
    conn: sqlite3.Connection, *, user_id: str, path: str, limit: int = 20
) -> list[RetrievalHit]:
    """Events whose payload references a given file path.

    v0.3.0 uses a LIKE scan on the JSON payload for simplicity; later
    versions can swap to a structured path index without changing callers.
    ``%`` / ``_`` in the path are escaped so file names like
    ``utils_v2.py`` match literally.
    """
    if not path:
        return []
    like = f"%{_like_escape(path)}%"
    placeholders = ",".join("?" * len(FILE_EVENT_TYPES))
    rows = query(
        conn,
        f"SELECT * FROM events WHERE user_id = ? AND event_type IN ({placeholders}) "
        "AND payload_json LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT ?",
        (user_id, *FILE_EVENT_TYPES, like, limit),
    )
    hits: list[RetrievalHit] = []
    for r in _to_dicts(rows):
        hits.append(
            _event_to_hit(
                r,
                reason=f"by_file match on events.payload_json LIKE %{path}%",
                score_components={
                    "keyword": 0.6,
                    "recency": 0.3 * _recency_score(r.get("created_at", "")),
                    "source": 0.1,
                },
            )
        )
    return hits


def by_decision(
    conn: sqlite3.Connection, *, user_id: str, limit: int = 20
) -> list[RetrievalHit]:
    """Claim state-changes + decision-family events.

    When the event targets a ``claim`` row, the hit title is enriched with
    the claim's ``subject predicate object`` so injected reminders read as
    "P is ok" rather than the opaque event id.
    """
    rows = query(
        conn,
        "SELECT * FROM events WHERE user_id = ? "
        "AND (event_type = 'claim.state_changed' OR event_type LIKE 'decision.%') "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    dicts = _to_dicts(rows)
    # Batch-fetch all referenced claims in one query to avoid N+1.
    claim_ids = [
        r["target_id"]
        for r in dicts
        if r.get("target_kind") == "claim" and r.get("target_id")
    ]
    spo_by_id: dict[str, str] = {}
    if claim_ids:
        placeholders = ",".join("?" * len(claim_ids))
        claim_rows = query(
            conn,
            f"SELECT claim_id, subject, predicate, object FROM claims "
            f"WHERE claim_id IN ({placeholders})",
            tuple(claim_ids),
        )
        for c in claim_rows:
            spo = f"{c['subject']} {c['predicate']} {c['object']}".strip()
            if spo:
                spo_by_id[c["claim_id"]] = spo

    hits: list[RetrievalHit] = []
    for r in dicts:
        hit = _event_to_hit(
            r,
            reason="by_decision match on event_type claim.state_changed OR decision.*",
            score_components={
                "keyword": 0.5,
                "recency": 0.5 * _recency_score(r.get("created_at", "")),
            },
        )
        spo = spo_by_id.get(r.get("target_id") or "")
        if spo:
            hit = dataclasses.replace(hit, title=f"{spo} — {hit.title}")
        hits.append(hit)
    return hits


def by_bug_fix(
    conn: sqlite3.Connection, *, user_id: str, limit: int = 20
) -> list[RetrievalHit]:
    """Events + claims whose text matches fix/bug tokens."""
    hits: list[RetrievalHit] = []

    like_clauses = " OR ".join(["payload_json LIKE ?"] * len(_FIX_TOKENS))
    like_params = tuple(f"%{tok}%" for tok in _FIX_TOKENS)
    erows = query(
        conn,
        f"SELECT * FROM events WHERE user_id = ? AND ({like_clauses}) "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, *like_params, limit),
    )
    for r in _to_dicts(erows):
        hits.append(
            _event_to_hit(
                r,
                reason="by_bug_fix match on events.payload_json LIKE fix/bug tokens",
                score_components={
                    "keyword": 0.6,
                    "recency": 0.4 * _recency_score(r.get("created_at", "")),
                },
            )
        )

    # One query with every (subject|predicate|object) x token LIKE OR'd together,
    # instead of len(_FIX_TOKENS) round-trips each returning up to `limit` rows.
    claim_or = " OR ".join(
        ["subject LIKE ?", "predicate LIKE ?", "object LIKE ?"] * len(_FIX_TOKENS)
    )
    claim_params: list[str] = []
    for tok in _FIX_TOKENS:
        like = f"%{tok}%"
        claim_params.extend([like, like, like])
    # ORDER BY is mandatory — without it SQLite returns heap-order rows and
    # LIMIT ? silently drops high-confidence claims at rowid > limit (BUG 2,
    # v0.5.0-pre1). claim_id ASC is the deterministic tiebreak.
    claim_rows = query(
        conn,
        f"SELECT * FROM claims WHERE user_id = ? AND ({claim_or}) "
        "ORDER BY confidence DESC, updated_at DESC, claim_id ASC LIMIT ?",
        (user_id, *claim_params, limit),
    )
    seen: set[str] = set()
    for r in _to_dicts(claim_rows):
        cid = r.get("claim_id", "")
        if cid in seen:
            continue
        seen.add(cid)
        hits.append(
            _claim_to_hit(
                r,
                reason="by_bug_fix match on claims.(subject|predicate|object) LIKE fix/bug",
                score_components={"keyword": 0.7, "source": 0.1},
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def by_timeline(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    since: str,
    until: str,
    limit: int = 50,
) -> list[RetrievalHit]:
    """Events in a timestamp window [since, until] ordered ascending."""
    try:
        since_norm = _iso_normalize(since, kind="since")
        until_norm = _iso_normalize(until, kind="until")
    except ValueError as exc:
        raise ValueError(f"by_timeline: could not parse since/until ISO-8601 ({exc})") from exc
    if since_norm > until_norm:
        raise ValueError(
            f"by_timeline: since ({since}) must be <= until ({until})"
        )
    rows = query(
        conn,
        "SELECT * FROM events WHERE user_id = ? AND created_at >= ? AND created_at <= ? "
        "ORDER BY created_at ASC LIMIT ?",
        (user_id, since_norm, until_norm, limit),
    )
    hits: list[RetrievalHit] = []
    for r in _to_dicts(rows):
        hits.append(
            _event_to_hit(
                r,
                reason=f"by_timeline match on created_at BETWEEN {since!r} AND {until!r}",
                score_components={"recency": _recency_score(r.get("created_at", ""))},
            )
        )
    return hits


def by_entity(
    conn: sqlite3.Connection, *, user_id: str, subject: str, limit: int = 20
) -> list[RetrievalHit]:
    """Claims whose subject matches (exact + case-insensitive prefix) + events
    whose payload references the subject."""
    if not subject:
        return []
    hits: list[RetrievalHit] = []

    prefix_like = f"{_like_escape(subject.lower())}%"
    # ORDER BY is mandatory — see by_bug_fix claim SELECT above (BUG 2).
    claim_rows = query(
        conn,
        "SELECT * FROM claims WHERE user_id = ? AND "
        "(subject = ? OR LOWER(subject) LIKE ? ESCAPE '\\') "
        "ORDER BY confidence DESC, updated_at DESC, claim_id ASC LIMIT ?",
        (user_id, subject, prefix_like, limit),
    )
    for r in _to_dicts(claim_rows):
        exact = r.get("subject") == subject
        hits.append(
            _claim_to_hit(
                r,
                reason=f"by_entity match on claims.subject "
                       f"{'exact' if exact else 'prefix'} {subject!r}",
                score_components={
                    "keyword": 1.0 if exact else 0.6,
                    "source": 0.1,
                },
            )
        )

    event_rows = query(
        conn,
        "SELECT * FROM events WHERE user_id = ? AND payload_json LIKE ? ESCAPE '\\' "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, f"%{_like_escape(subject)}%", limit),
    )
    for r in _to_dicts(event_rows):
        hits.append(
            _event_to_hit(
                r,
                reason=f"by_entity match on events.payload_json LIKE %{subject}%",
                score_components={
                    "keyword": 0.5,
                    "recency": 0.3 * _recency_score(r.get("created_at", "")),
                },
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
