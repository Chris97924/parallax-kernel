"""Tests for parallax.injector — SessionStart <system-reminder> builder."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.events import record_event
from parallax.hooks import ingest_hook
from parallax.ingest import ingest_claim
from parallax.injector import MAX_REMINDER_CHARS, build_session_reminder
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "inj.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


class TestBuildSessionReminder:
    def test_empty_db_renders_placeholders(self, conn: sqlite3.Connection) -> None:
        text = build_session_reminder(conn, user_id="u")
        assert text.startswith("<system-reminder>")
        assert text.endswith("</system-reminder>")
        assert "Recently modified files:" in text
        assert "Last 3 decisions:" in text
        assert "(none)" in text

    def test_file_edits_surface_paths(self, conn: sqlite3.Connection) -> None:
        ingest_hook(
            conn,
            hook_type="SessionStart",
            session_id="s1",
            payload={},
            user_id="u",
        )
        for p in ("parallax/hooks.py", "parallax/retrieve.py", "tests/t.py"):
            ingest_hook(
                conn,
                hook_type="PostToolUse",
                session_id="s1",
                payload={"tool_name": "Edit", "tool_input": {"file_path": p}},
                user_id="u",
            )
        text = build_session_reminder(conn, user_id="u")
        assert "parallax/hooks.py" in text
        assert "parallax/retrieve.py" in text

    def test_decisions_surface(self, conn: sqlite3.Connection) -> None:
        claim_id = ingest_claim(
            conn, user_id="u", subject="P", predicate="is", object_="ok"
        )
        for state in ("confirmed", "rejected", "confirmed"):
            record_event(
                conn,
                user_id="u",
                actor="system",
                event_type="claim.state_changed",
                target_kind="claim",
                target_id=claim_id,
                payload={"from": "pending", "to": state},
            )
        text = build_session_reminder(conn, user_id="u")
        assert "P is ok" in text

    def test_cap_truncates(self, conn: sqlite3.Connection) -> None:
        # Force a bulky decision stream so the render exceeds the cap.
        claim_id = ingest_claim(
            conn, user_id="u", subject="VeryLongSubject" * 5, predicate="p", object_="o"
        )
        # Seed ~100 decision events via direct record_event (distinct payloads).
        for i in range(120):
            record_event(
                conn,
                user_id="u",
                actor="system",
                event_type="claim.state_changed",
                target_kind="claim",
                target_id=claim_id,
                payload={"from": "pending", "to": "confirmed", "idx": i, "pad": "x" * 80},
            )
        # Also seed many file edits under a session so that section bulks up.
        ingest_hook(
            conn,
            hook_type="SessionStart",
            session_id="s1",
            payload={},
            user_id="u",
        )
        for i in range(50):
            ingest_hook(
                conn,
                hook_type="PostToolUse",
                session_id="s1",
                payload={
                    "tool_name": "Edit",
                    "tool_input": {"file_path": f"f/super_long_name_{i}_" + "x" * 50 + ".py"},
                },
                user_id="u",
            )
        text = build_session_reminder(conn, user_id="u", session_id="s1", max_hits=40)
        # Must stay under the cap AND surface the explicit truncation marker
        # so downstream consumers can detect that sections were trimmed.
        assert len(text) <= MAX_REMINDER_CHARS
        assert "... (truncated)" in text


class TestCliInject:
    def test_cli_matches_function(
        self,
        conn: sqlite3.Connection,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Use the fixture's DB path by redirecting load_config via env var.
        db_path = pathlib.Path(str(conn.execute("PRAGMA database_list").fetchone()[2]))
        ingest_hook(
            conn,
            hook_type="SessionStart",
            session_id="sX",
            payload={},
            user_id="u",
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("PARALLAX_DB_PATH", str(db_path))
        monkeypatch.setenv("PARALLAX_USER_ID", "u")

        from parallax.cli import main

        rc = main(["inspect", "inject", "--session", "sX", "--max", "4"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert captured.startswith("<system-reminder>")
        assert "</system-reminder>" in captured
