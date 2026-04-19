"""Tests for the retrieval --explain trace view (v0.5.0-pre5).

Four layers of coverage:

* ``TestTraceDataclasses`` — shape and immutability of RetrievalTrace /
  RetrievalTraceStage.
* ``TestInstrumentedFunctions`` — each v0.3.0 retrieve function populates the
  private _TraceBuilder when one is passed in, and is byte-identical when not.
* ``TestExplainRetrieve`` — the public dispatcher wires up the six kinds and
  raises on bad input.
* ``TestNearMissSampler`` — the zero-hit diagnostic note for entity/file/bug.
"""

from __future__ import annotations

import dataclasses
import pathlib
import sqlite3

import pytest

from parallax.events import record_event
from parallax.hooks import ingest_hook
from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.retrieve import (
    RetrievalTrace,
    RetrievalTraceStage,
    by_entity,
    by_file,
    by_timeline,
    explain_retrieve,
    recent_context,
)
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "retr_explain.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


def _seed_basic(conn: sqlite3.Connection) -> None:
    ingest_hook(
        conn,
        hook_type="SessionStart",
        session_id="s1",
        payload={},
        user_id="u",
    )
    ingest_hook(
        conn,
        hook_type="PostToolUse",
        session_id="s1",
        payload={"tool_name": "Edit", "tool_input": {"file_path": "parallax/retrieve.py"}},
        user_id="u",
    )
    ingest_claim(conn, user_id="u", subject="Parallax", predicate="is", object_="kb")


class TestTraceDataclasses:
    def test_stage_is_frozen(self) -> None:
        s = RetrievalTraceStage(
            name="user_scope", candidates_in=10, candidates_out=5, detail="u=u"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.name = "mutated"  # type: ignore[misc]

    def test_trace_is_frozen_with_tuple_fields(self) -> None:
        t = RetrievalTrace(
            kind="entity",
            params={"user_id": "u"},
            normalized_params={},
            sql_fragments=("SELECT 1",),
            stages=(
                RetrievalTraceStage(
                    name="final", candidates_in=0, candidates_out=0, detail=""
                ),
            ),
            notes=("hello",),
            hits=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.kind = "other"  # type: ignore[misc]
        # tuple-typed fields are not lists
        assert isinstance(t.sql_fragments, tuple)
        assert isinstance(t.stages, tuple)
        assert isinstance(t.notes, tuple)
        assert isinstance(t.hits, tuple)

    def test_builder_freeze_coerces_lists_to_tuples(self) -> None:
        # _TraceBuilder is private: reach through module for a direct unit.
        from parallax.retrieve import _TraceBuilder  # type: ignore[attr-defined]

        b = _TraceBuilder(kind="entity", params={"user_id": "u"})
        b.sql("SELECT * FROM claims WHERE user_id = ?")
        b.stage("user_scope", candidates_in=3, candidates_out=3, detail="u=u")
        b.stage("final", candidates_in=3, candidates_out=0, detail="limit=10")
        b.note("no subject matched")
        b.set_normalized({"subject_lower": "parallax"})
        t = b.freeze(hits=())
        assert isinstance(t, RetrievalTrace)
        assert isinstance(t.stages, tuple) and len(t.stages) == 2
        assert isinstance(t.sql_fragments, tuple) and t.sql_fragments[0].startswith("SELECT")
        assert t.notes == ("no subject matched",)
        assert t.normalized_params == {"subject_lower": "parallax"}


class TestInstrumentedFunctions:
    """Each retrieve function accepts _trace=_TraceBuilder() without changing
    its public return value; when _trace is None, behaviour is unchanged."""

    def test_by_timeline_records_normalized_since_until(
        self, conn: sqlite3.Connection
    ) -> None:
        from parallax.retrieve import _TraceBuilder  # type: ignore[attr-defined]

        _seed_basic(conn)
        b = _TraceBuilder(kind="timeline", params={"user_id": "u"})
        hits = by_timeline(
            conn,
            user_id="u",
            since="2020-01-01T00:00:00Z",
            until="2099-01-01T00:00:00Z",
            _trace=b,
        )
        t = b.freeze(hits=tuple(hits))
        assert "since_norm" in t.normalized_params
        assert "until_norm" in t.normalized_params
        # until microsecond-inclusion invariant from _iso_normalize
        assert t.normalized_params["until_norm"].endswith(".999999+00:00")

    def test_recent_context_fallback_note_on_missing_session_start(
        self, conn: sqlite3.Connection
    ) -> None:
        from parallax.retrieve import _TraceBuilder  # type: ignore[attr-defined]

        # Seed only a non-session.start event so session.start lookup fails.
        record_event(
            conn,
            user_id="u",
            actor="system",
            event_type="marker",
            target_kind=None,
            target_id=None,
            payload={},
        )
        b = _TraceBuilder(kind="recent", params={"user_id": "u"})
        hits = recent_context(conn, user_id="u", _trace=b)
        t = b.freeze(hits=tuple(hits))
        joined = "\n".join(t.notes)
        assert "no session.start" in joined.lower()

    def test_by_entity_stages_include_scopes_and_final(
        self, conn: sqlite3.Connection
    ) -> None:
        from parallax.retrieve import _TraceBuilder  # type: ignore[attr-defined]

        _seed_basic(conn)
        b = _TraceBuilder(kind="entity", params={"user_id": "u"})
        hits = by_entity(conn, user_id="u", subject="Parallax", _trace=b)
        t = b.freeze(hits=tuple(hits))
        names = [s.name for s in t.stages]
        # Parallel claim+event subscans → two user_scope_* stages plus 'final'.
        assert "user_scope_claims" in names
        assert "user_scope_events" in names
        assert names[-1] == "final"
        # 'final.candidates_out' is the hit count, never greater than the
        # combined corpus (tightest invariant we can assert across parallel scans).
        final = t.stages[-1]
        assert final.candidates_out == len(hits)
        assert final.candidates_out <= final.candidates_in

    def test_by_file_empty_path_records_skip_note(
        self, conn: sqlite3.Connection
    ) -> None:
        from parallax.retrieve import _TraceBuilder  # type: ignore[attr-defined]

        b = _TraceBuilder(kind="file", params={"user_id": "u", "path": ""})
        hits = by_file(conn, user_id="u", path="", _trace=b)
        assert hits == []
        t = b.freeze(hits=())
        joined = "\n".join(t.notes).lower()
        assert "empty path" in joined

    def test_none_trace_is_byte_identical(self, conn: sqlite3.Connection) -> None:
        """Sanity: passing _trace=None must not change return values.

        Compare every field except ``score`` (recency has a ``now()`` term that
        moves between two calls a few ms apart) and the ``recency`` entry inside
        ``explain.score_components``. The rest — entity_kind/id, title, evidence,
        full row, reason, and non-recency score components — must be identical so
        a future regression that changes structure on the None path can't slip past.
        """
        _seed_basic(conn)
        a = by_entity(conn, user_id="u", subject="Parallax")
        b = by_entity(conn, user_id="u", subject="Parallax", _trace=None)
        assert len(a) == len(b)
        for ha, hb in zip(a, b, strict=True):
            assert ha.entity_kind == hb.entity_kind
            assert ha.entity_id == hb.entity_id
            assert ha.title == hb.title
            assert ha.evidence == hb.evidence
            assert ha.full == hb.full
            assert ha.explain["reason"] == hb.explain["reason"]
            sc_a = {k: v for k, v in ha.explain["score_components"].items() if k != "recency"}
            sc_b = {k: v for k, v in hb.explain["score_components"].items() if k != "recency"}
            assert sc_a == sc_b


class TestExplainRetrieve:
    def test_each_kind_returns_trace_matching_direct_call(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_basic(conn)
        # entity
        direct = by_entity(conn, user_id="u", subject="Parallax")
        t = explain_retrieve(conn, kind="entity", user_id="u", query_text="Parallax")
        assert [h.entity_id for h in t.hits] == [h.entity_id for h in direct]
        # file
        direct = by_file(conn, user_id="u", path="parallax/retrieve.py")
        t = explain_retrieve(
            conn, kind="file", user_id="u", query_text="parallax/retrieve.py"
        )
        assert [h.entity_id for h in t.hits] == [h.entity_id for h in direct]
        # recent / decision / bug / timeline
        for kind in ("recent", "decision", "bug"):
            t = explain_retrieve(conn, kind=kind, user_id="u")
            assert isinstance(t, RetrievalTrace)
        t = explain_retrieve(
            conn,
            kind="timeline",
            user_id="u",
            since="2020-01-01T00:00:00Z",
            until="2099-01-01T00:00:00Z",
        )
        assert isinstance(t, RetrievalTrace)

    def test_unknown_kind_raises(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="martians"):
            explain_retrieve(conn, kind="martians", user_id="u")

    def test_timeline_requires_since_until(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="since|until"):
            explain_retrieve(conn, kind="timeline", user_id="u")


class TestNearMissSampler:
    def test_entity_miss_returns_up_to_3_sample_notes(
        self, conn: sqlite3.Connection
    ) -> None:
        for i in range(5):
            ingest_claim(
                conn,
                user_id="u",
                subject=f"Other{i}",
                predicate="is",
                object_=f"val{i}",
            )
        t = explain_retrieve(
            conn, kind="entity", user_id="u", query_text="NoSuchSubject"
        )
        assert len(t.hits) == 0
        near = [n for n in t.notes if n.startswith("near_miss(entity)")]
        assert len(near) == 3

    def test_entity_empty_corpus_returns_corpus_empty_note(
        self, conn: sqlite3.Connection
    ) -> None:
        t = explain_retrieve(
            conn, kind="entity", user_id="nobody", query_text="Anything"
        )
        empty_notes = [
            n for n in t.notes if "corpus empty" in n and "entity" in n
        ]
        assert len(empty_notes) == 1

    def test_file_miss_returns_sample_notes(self, conn: sqlite3.Connection) -> None:
        ingest_hook(
            conn,
            hook_type="SessionStart",
            session_id="s1",
            payload={},
            user_id="u",
        )
        for i in range(4):
            ingest_hook(
                conn,
                hook_type="PostToolUse",
                session_id="s1",
                payload={"tool_name": "Edit", "tool_input": {"file_path": f"f{i}.py"}},
                user_id="u",
            )
        t = explain_retrieve(
            conn, kind="file", user_id="u", query_text="does_not_exist.py"
        )
        assert len(t.hits) == 0
        near = [n for n in t.notes if n.startswith("near_miss(file)")]
        assert len(near) == 3

    def test_near_miss_absent_when_hits_present(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_basic(conn)
        t = explain_retrieve(conn, kind="entity", user_id="u", query_text="Parallax")
        assert len(t.hits) >= 1
        near = [n for n in t.notes if n.startswith("near_miss(")]
        assert near == []
