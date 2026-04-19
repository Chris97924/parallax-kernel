"""Regression tests for the Windows cp950 UnicodeEncodeError crash.

Repro: on a cmd.exe with active codepage cp950 (Traditional Chinese), running
``parallax inspect retrieve`` and hitting a row with CJK in the title crashes
with UnicodeEncodeError before any output lands. The fix reconfigures stdout
and stderr to UTF-8 at the top of ``main()`` so the CLI is robust regardless
of the parent shell's codepage.
"""

from __future__ import annotations

import io
import pathlib
import sys

import pytest

from parallax import cli as cli_mod
from parallax.cli import _ensure_utf8_streams, main
from parallax.ingest import ingest_claim, ingest_memory
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect


class _RecordingTextIOWrapper(io.TextIOWrapper):
    """Counts reconfigure() invocations so the skip-if-utf8 branch is testable."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.reconfigure_calls = 0

    def reconfigure(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        self.reconfigure_calls += 1
        super().reconfigure(*args, **kwargs)  # type: ignore[arg-type]


class TestEnsureUtf8Streams:
    def test_flips_cp950_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wrapper = io.TextIOWrapper(io.BytesIO(), encoding="cp950")
        monkeypatch.setattr(sys, "stdout", wrapper)
        _ensure_utf8_streams()
        assert sys.stdout.encoding.lower() == "utf-8"

    def test_tolerates_non_reconfigurable_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sio = io.StringIO()
        monkeypatch.setattr(sys, "stdout", sio)
        _ensure_utf8_streams()
        assert sys.stdout is sio

    def test_skips_when_already_utf8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wrapper = _RecordingTextIOWrapper(io.BytesIO(), encoding="utf-8")
        monkeypatch.setattr(sys, "stdout", wrapper)
        _ensure_utf8_streams()
        assert wrapper.reconfigure_calls == 0
        assert sys.stdout.encoding.lower() == "utf-8"

    def test_tolerates_reconfigure_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Hostile:
            encoding = "cp950"

            def reconfigure(self, **_: object) -> None:
                raise LookupError("no such codec")

        monkeypatch.setattr(sys, "stdout", _Hostile())
        _ensure_utf8_streams()

    def test_reconfigures_stderr_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout_rec = io.TextIOWrapper(io.BytesIO(), encoding="cp950")
        stderr_rec = io.TextIOWrapper(io.BytesIO(), encoding="cp950")
        monkeypatch.setattr(sys, "stdout", stdout_rec)
        monkeypatch.setattr(sys, "stderr", stderr_rec)
        _ensure_utf8_streams()
        assert stderr_rec.encoding.lower() == "utf-8"


class TestMainOnCp950Stdout:
    """Pins the test to a cp1252 stream so CJK is guaranteed unencodable —
    same failure class the user hits on cp950 for chars outside Big5."""

    def _seed_cjk_db(self, db: pathlib.Path) -> None:
        c = connect(db)
        migrate_to_latest(c)
        ingest_memory(
            c,
            user_id="u",
            title="專案X 已上線",
            summary="LongMemEval 驗收完成",
            vault_path="/notes/專案X.md",
        )
        ingest_claim(
            c,
            user_id="u",
            subject="專案X",
            predicate="狀態",
            object_="已上線",
        )
        c.commit()
        c.close()

    def test_main_prints_cjk_without_crash(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db = tmp_path / "cp950.db"
        self._seed_cjk_db(db)

        monkeypatch.setenv("PARALLAX_DB_PATH", str(db))
        monkeypatch.setenv("PARALLAX_USER_ID", "u")

        legacy_stdout = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1252", errors="strict"
        )
        legacy_stderr = io.TextIOWrapper(
            io.BytesIO(), encoding="cp1252", errors="strict"
        )
        monkeypatch.setattr(cli_mod.sys, "stdout", legacy_stdout)
        monkeypatch.setattr(cli_mod.sys, "stderr", legacy_stderr)

        rc = main(
            ["inspect", "retrieve", "專案X", "--kind", "entity", "--level", "1"]
        )

        assert rc == 0
        assert cli_mod.sys.stdout.encoding.lower() == "utf-8"
        cli_mod.sys.stdout.flush()
        written = legacy_stdout.buffer.getvalue().decode("utf-8", errors="replace")
        assert "專案X" in written
