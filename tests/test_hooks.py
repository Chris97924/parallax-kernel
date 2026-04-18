"""Tests for parallax.hooks — Claude Code hook → events ingestion."""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from parallax.hooks import ingest_from_json, ingest_hook
from parallax.ingest import ingest_memory
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "hooks.db"
    c = connect(db)
    migrate_to_latest(c)
    yield c
    c.close()


def _fetch_event(conn: sqlite3.Connection, event_id: str) -> dict:
    row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    assert row is not None
    return dict(row)


class TestIngestHookSession:
    def test_session_start_round_trip(self, conn: sqlite3.Connection) -> None:
        eid = ingest_hook(
            conn,
            hook_type="SessionStart",
            session_id="sess-1",
            payload={"source": "startup"},
            user_id="u",
        )
        row = _fetch_event(conn, eid)
        assert row["event_type"] == "session.start"
        assert row["session_id"] == "sess-1"
        assert json.loads(row["payload_json"])["source"] == "startup"

    def test_session_end_alias(self, conn: sqlite3.Connection) -> None:
        eid_end = ingest_hook(
            conn, hook_type="SessionEnd", session_id="sess-1", payload={}, user_id="u"
        )
        eid_stop = ingest_hook(
            conn, hook_type="Stop", session_id="sess-1", payload={}, user_id="u"
        )
        assert _fetch_event(conn, eid_end)["event_type"] == "session.stop"
        assert _fetch_event(conn, eid_stop)["event_type"] == "session.stop"


class TestIngestHookTools:
    def test_pre_tool_bash(self, conn: sqlite3.Connection) -> None:
        eid = ingest_hook(
            conn,
            hook_type="PreToolUse",
            session_id="s1",
            payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
            user_id="u",
        )
        row = _fetch_event(conn, eid)
        assert row["event_type"] == "tool.bash"
        assert "ls" in row["payload_json"]

    def test_post_tool_edit_untracked(self, conn: sqlite3.Connection) -> None:
        eid = ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/new.py"},
            },
            user_id="u",
        )
        row = _fetch_event(conn, eid)
        assert row["event_type"] == "file.edit"
        assert row["target_kind"] is None
        assert row["target_id"] is None
        assert "_path_sha16" in row["payload_json"]

    def test_post_tool_edit_tracked_memory(self, conn: sqlite3.Connection) -> None:
        mem_id = ingest_memory(
            conn,
            user_id="u",
            source_id=None,
            vault_path="users/u/memories/abc.md",
            title="abc",
            summary="tracked file",
        )
        eid = ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={
                "tool_name": "Edit",
                "tool_input": {"file_path": "users/u/memories/abc.md"},
            },
            user_id="u",
        )
        row = _fetch_event(conn, eid)
        assert row["event_type"] == "file.edit"
        assert row["target_kind"] == "memory"
        assert row["target_id"] == mem_id

    def test_post_tool_write_untracked(self, conn: sqlite3.Connection) -> None:
        eid = ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/fresh.py"},
            },
            user_id="u",
        )
        row = _fetch_event(conn, eid)
        assert row["event_type"] == "file.edit"
        assert row["target_kind"] is None
        assert "_path_sha16" in row["payload_json"]

    def test_post_tool_multiedit_untracked(self, conn: sqlite3.Connection) -> None:
        eid = ingest_hook(
            conn,
            hook_type="PostToolUse",
            session_id="s1",
            payload={
                "tool_name": "MultiEdit",
                "tool_input": {"file_path": "/tmp/multi.py"},
            },
            user_id="u",
        )
        row = _fetch_event(conn, eid)
        assert row["event_type"] == "file.edit"
        assert "_path_sha16" in row["payload_json"]

    def test_pre_tool_write_maps_to_tool_write(self, conn: sqlite3.Connection) -> None:
        eid = ingest_hook(
            conn,
            hook_type="PreToolUse",
            session_id="s1",
            payload={"tool_name": "Write", "tool_input": {"file_path": "/tmp/x.py"}},
            user_id="u",
        )
        assert _fetch_event(conn, eid)["event_type"] == "tool.write"

    def test_pre_tool_missing_tool_name(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="tool_name"):
            ingest_hook(
                conn,
                hook_type="PreToolUse",
                session_id="s1",
                payload={},
                user_id="u",
            )


class TestIngestHookValidation:
    def test_missing_hook_type(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="hook_type"):
            ingest_hook(conn, hook_type="", session_id="s1", payload={}, user_id="u")

    def test_missing_session_id(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="session_id"):
            ingest_hook(
                conn, hook_type="SessionStart", session_id="", payload={}, user_id="u"
            )

    def test_non_mapping_payload(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="mapping"):
            ingest_hook(
                conn,
                hook_type="SessionStart",
                session_id="s1",
                payload="oops",  # type: ignore[arg-type]
                user_id="u",
            )

    def test_unknown_hook_type(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="unknown hook_type"):
            ingest_hook(
                conn,
                hook_type="TotallyMadeUp",
                session_id="s1",
                payload={},
                user_id="u",
            )


class TestIngestFromJson:
    def test_session_start_envelope(self, conn: sqlite3.Connection) -> None:
        raw = json.dumps(
            {
                "hook_event_name": "SessionStart",
                "session_id": "s99",
                "payload": {"source": "resume"},
            }
        )
        eid = ingest_from_json(conn, user_id="u", raw_json=raw)
        row = _fetch_event(conn, eid)
        assert row["session_id"] == "s99"
        assert row["event_type"] == "session.start"

    def test_invalid_json(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid JSON"):
            ingest_from_json(conn, user_id="u", raw_json="{not json")

    def test_missing_envelope_field(self, conn: sqlite3.Connection) -> None:
        raw = json.dumps({"hook_event_name": "SessionStart"})
        with pytest.raises(ValueError, match="session_id"):
            ingest_from_json(conn, user_id="u", raw_json=raw)

    def test_user_prompt_submit(self, conn: sqlite3.Connection) -> None:
        raw = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "payload": {"prompt": "hello"},
            }
        )
        eid = ingest_from_json(conn, user_id="u", raw_json=raw)
        assert _fetch_event(conn, eid)["event_type"] == "prompt.submit"
