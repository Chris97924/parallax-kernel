"""Schema-level regression tests for Parallax DDL.

Backs ADR-003 (events.target_kind unconstrained vs decisions.target_kind
hard CHECK): asserts the DB-level CHECK on decisions.target_kind rejects
'decision' (and any other kind outside {claim, memory, source}), and
that events.target_kind still accepts 'decision' for audit rows.
"""

from __future__ import annotations

import sqlite3

import pytest


def _minimal_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources(source_id, uri, kind, content_hash, user_id, "
        "ingested_at, state) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("src-1", "direct:u", "chat", "0" * 64, "u", "2026-04-18T00:00:00Z", "ingested"),
    )
    conn.commit()


def _insert_decision(conn: sqlite3.Connection, target_kind: str) -> None:
    conn.execute(
        "INSERT INTO decisions(decision_id, user_id, target_kind, target_id, "
        "action, actor, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("d-1", "u", target_kind, "tgt-1", "confirm", "user", "proposed",
         "2026-04-18T00:00:00Z"),
    )


class TestDecisionsTargetKindCheck:
    """decisions.target_kind CHECK (target_kind IN ('claim','memory','source'))."""

    @pytest.mark.parametrize("kind", ["claim", "memory", "source"])
    def test_allowed_kinds_insert(
        self, conn: sqlite3.Connection, kind: str
    ) -> None:
        _minimal_source(conn)
        _insert_decision(conn, kind)
        conn.commit()

    def test_decision_target_kind_rejected(
        self, conn: sqlite3.Connection
    ) -> None:
        _minimal_source(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_decision(conn, "decision")

    def test_unknown_target_kind_rejected(
        self, conn: sqlite3.Connection
    ) -> None:
        _minimal_source(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_decision(conn, "foo")


class TestEventsTargetKindUnconstrained:
    """events.target_kind has no CHECK; decision-level audit rows must insert."""

    def test_events_accepts_decision_target_kind(
        self, conn: sqlite3.Connection
    ) -> None:
        conn.execute(
            "INSERT INTO events(event_id, user_id, actor, event_type, "
            "target_kind, target_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("e-1", "u", "system", "decision.state_changed",
             "decision", "d-1", "{}", "2026-04-18T00:00:00Z"),
        )
        conn.commit()
