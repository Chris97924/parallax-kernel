"""Tests for `parallax inspect` CLI subcommands."""

from __future__ import annotations

import pathlib

import pytest

from parallax.cli import main
from parallax.events import record_event
from parallax.hooks import ingest_hook
from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect


@pytest.fixture()
def seeded_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "cli.db"
    c = connect(db)
    migrate_to_latest(c)
    ingest_hook(c, hook_type="SessionStart", session_id="s1", payload={}, user_id="u")
    ingest_hook(
        c,
        hook_type="PreToolUse",
        session_id="s1",
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
        user_id="u",
    )
    ingest_hook(
        c,
        hook_type="PostToolUse",
        session_id="s1",
        payload={"tool_name": "Edit", "tool_input": {"file_path": "parallax/retrieve.py"}},
        user_id="u",
    )
    claim_id = ingest_claim(
        c, user_id="u", subject="ProjectX", predicate="is", object_="shipped"
    )
    record_event(
        c,
        user_id="u",
        actor="system",
        event_type="claim.state_changed",
        target_kind="claim",
        target_id=claim_id,
        payload={"from": "pending", "to": "confirmed"},
    )
    c.commit()
    c.close()
    return db


@pytest.fixture(autouse=True)
def _set_env(
    monkeypatch: pytest.MonkeyPatch, seeded_db: pathlib.Path
) -> None:
    monkeypatch.setenv("PARALLAX_DB_PATH", str(seeded_db))
    monkeypatch.setenv("PARALLAX_USER_ID", "u")


class TestInspectEvents:
    def test_with_session_lists_rows(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["inspect", "events", "--session", "s1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "session.start" in out
        assert "tool.bash" in out
        assert "file.edit" in out

    def test_without_session_lists_sessions(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["inspect", "events"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Recent sessions:" in out
        assert "s1" in out

    def test_missing_session_user_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["inspect", "events", "--session", "does-not-exist"])
        assert rc == 1


class TestInspectRetrieve:
    def test_explain_prints_score_components(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["inspect", "retrieve", "--kind", "recent", "--explain"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "score_components" in out
        assert "reason" in out

    def test_level_three_shows_full(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["inspect", "retrieve", "--kind", "recent", "--level", "3"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "full:" in out

    def test_unknown_kind(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["inspect", "retrieve", "--kind", "martians"])
        assert rc == 1

    def test_entity_default_with_query(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["inspect", "retrieve", "ProjectX"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ProjectX" in out

    def test_file_kind(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["inspect", "retrieve", "parallax/retrieve.py", "--kind", "file"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "file.edit" in out or "tool.edit" in out
