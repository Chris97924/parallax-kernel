"""Regression tests for parallax CLI outer-guard handling.

Three crash classes are covered:

1. ``BrokenPipeError`` when stdout is closed mid-print (``| head``).
2. ``KeyboardInterrupt`` from Ctrl+C — must exit 130, not traceback.
3. Any other unexpected ``Exception`` — formatted as a one-line
   ``parallax: <msg>`` on stderr instead of a full traceback.

``SystemExit`` MUST continue to propagate so argparse ``--help`` still
exits 0 and usage errors still exit 2.
"""

from __future__ import annotations

import io
import pathlib
import sys

import pytest

from parallax import cli as cli_mod
from parallax.cli import (
    _EXIT_INTERRUPTED,
    _EXIT_OK,
    _EXIT_USAGE,
    _EXIT_USER_ERROR,
    _silence_broken_pipe,
    main,
)
from parallax.events import record_event
from parallax.hooks import ingest_hook
from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect


class TestOuterGuard:
    def test_keyboard_interrupt_returns_130(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_argv: object) -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_mod, "_dispatch", _boom)
        assert main([]) == _EXIT_INTERRUPTED

    def test_broken_pipe_returns_zero_and_silences_stdout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"n": 0}

        def _boom(_argv: object) -> int:
            raise BrokenPipeError

        def _fake_silence() -> None:
            calls["n"] += 1

        monkeypatch.setattr(cli_mod, "_dispatch", _boom)
        monkeypatch.setattr(cli_mod, "_silence_broken_pipe", _fake_silence)
        rc = main([])
        assert rc == _EXIT_OK
        assert calls["n"] == 1

    def test_unexpected_exception_returns_user_error_and_prints_to_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def _boom(_argv: object) -> int:
            raise RuntimeError("boom")

        monkeypatch.setattr(cli_mod, "_dispatch", _boom)
        rc = main([])
        captured = capsys.readouterr()
        assert rc == _EXIT_USER_ERROR
        assert "parallax: boom" in captured.err
        assert "Traceback" not in captured.err
        assert 'File "' not in captured.err
        assert captured.out == ""

    def test_system_exit_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_argv: object) -> int:
            raise SystemExit(2)

        monkeypatch.setattr(cli_mod, "_dispatch", _boom)
        with pytest.raises(SystemExit) as ei:
            main([])
        assert ei.value.code == 2

    def test_help_still_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as ei:
            main(["--help"])
        assert ei.value.code == 0


class TestSilenceBrokenPipe:
    def test_tolerates_no_fileno(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        # Must not raise — StringIO has no real fileno.
        _silence_broken_pipe()


@pytest.fixture()
def seeded_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "robust.db"
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


class TestDispatchHappyPath:
    def test_dispatch_happy_path_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        seeded_db: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("PARALLAX_DB_PATH", str(seeded_db))
        monkeypatch.setenv("PARALLAX_USER_ID", "u")
        rc = main(["inspect", "events", "--session", "s1"])
        assert rc == _EXIT_OK
        out = capsys.readouterr().out
        assert "session.start" in out


class _BrokenStderr:
    """Stderr stand-in whose first .write() raises BrokenPipeError.

    Has ``encoding`` so print()'s text-encoding path doesn't explode before
    it reaches .write(). Records whether write was attempted so the test
    can distinguish 'print never tried' from 'print tried and was caught'.
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self) -> None:
        self.write_called = False

    def write(self, _data: str) -> int:
        self.write_called = True
        raise BrokenPipeError

    def flush(self) -> None:
        pass


class TestStderrBrokenPipe:
    def test_broken_pipe_on_stderr_during_exception_arm_does_not_propagate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(_argv: object) -> int:
            raise RuntimeError("boom")

        broken = _BrokenStderr()
        silence_calls = {"n": 0}

        def _fake_silence() -> None:
            silence_calls["n"] += 1

        monkeypatch.setattr(cli_mod, "_dispatch", _boom)
        monkeypatch.setattr(cli_mod.sys, "stderr", broken)
        monkeypatch.setattr(cli_mod, "_silence_broken_pipe", _fake_silence)
        rc = main([])
        assert rc == _EXIT_USER_ERROR
        assert broken.write_called
        assert silence_calls["n"] == 1


class TestSubcommandHelp:
    def test_subcommand_help_still_exits_zero(self) -> None:
        with pytest.raises(SystemExit) as ei:
            main(["inspect", "--help"])
        assert ei.value.code == 0


class TestDispatchBareInvocation:
    def test_dispatch_bare_invocation_returns_usage(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli_mod._dispatch([])
        assert rc == _EXIT_USAGE
        err = capsys.readouterr().err
        assert "usage" in err.lower()
