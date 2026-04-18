"""Tests for migration 0006 — events.session_id column + indexes."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.migrations import (
    MIGRATIONS,
    migrate_down_to,
    migrate_to_latest,
)
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "m6.db"
    c = connect(db)
    yield c
    c.close()


class TestEventsSessionIdFix06:
    def test_registered_as_version_6(self) -> None:
        versions = [m.version for m in MIGRATIONS]
        names = {m.version: m.name for m in MIGRATIONS}
        assert 6 in versions
        assert names[6] == "events_session_id"

    def test_up_adds_column_and_indexes(self, conn: sqlite3.Connection) -> None:
        migrate_to_latest(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
        assert "session_id" in cols
        idxs = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            ).fetchall()
        }
        assert "idx_events_session" in idxs
        assert "idx_events_type_session" in idxs

    def test_down_drops_column_and_preserves_rows(
        self, conn: sqlite3.Connection
    ) -> None:
        migrate_to_latest(conn)
        conn.execute(
            """INSERT INTO events(event_id, user_id, actor, event_type, target_kind,
                   target_id, payload_json, approval_tier, created_at, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("e1", "u", "system", "demo", None, None, '{"x":1}',
             None, "2026-04-19T00:00:00Z", "s1"),
        )
        conn.commit()

        migrate_down_to(conn, 5)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
        assert "session_id" not in cols

        # Event row preserved through the table-swap with payload intact.
        row = conn.execute(
            "SELECT event_id, payload_json FROM events WHERE event_id = ?", ("e1",)
        ).fetchone()
        assert row["event_id"] == "e1"
        assert row["payload_json"] == '{"x":1}'

    def test_round_trip_re_enables_column(self, conn: sqlite3.Connection) -> None:
        migrate_to_latest(conn)
        migrate_down_to(conn, 5)
        applied = migrate_to_latest(conn)
        assert 6 in applied
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
        assert "session_id" in cols

    def test_append_only_triggers_survive_down(
        self, conn: sqlite3.Connection
    ) -> None:
        migrate_to_latest(conn)
        migrate_down_to(conn, 5)
        conn.execute(
            """INSERT INTO events(event_id, user_id, actor, event_type, target_kind,
                   target_id, payload_json, approval_tier, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("e2", "u", "s", "x", None, None, "{}", None, "2026-04-19T00:00:00Z"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
            conn.execute("UPDATE events SET event_type = 'mutated' WHERE event_id = 'e2'")
