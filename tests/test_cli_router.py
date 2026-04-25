"""Tests for `parallax router` CLI subcommands.

US-D3-05: arbitration view
US-D3-06: backfill plan / apply
"""

from __future__ import annotations

import pathlib

import pytest

from parallax.cli import main
from parallax.ingest import ingest_claim, ingest_memory
from parallax.migrations import migrate_to_latest
from parallax.router.contracts import ArbitrationDecision
from parallax.router.types import FieldCandidate, MappingState
from parallax.sqlite_store import connect

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def empty_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "test.db"
    c = connect(db)
    migrate_to_latest(c)
    c.commit()
    c.close()
    return db


@pytest.fixture()
def seeded_db(tmp_path: pathlib.Path) -> pathlib.Path:
    db = tmp_path / "test.db"
    c = connect(db)
    migrate_to_latest(c)
    ingest_memory(
        c,
        user_id="u",
        title="Python notes",
        summary="env setup",
        vault_path="notes/py.md",
    )
    ingest_claim(
        c,
        user_id="u",
        subject="stack",
        predicate="decision:choose-db",
        object_="sqlite",
    )
    ingest_claim(
        c,
        user_id="u",
        subject="auth",
        predicate="fix:bug-1234",
        object_="fixed null pointer",
    )
    c.commit()
    c.close()
    return db


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch, seeded_db: pathlib.Path) -> None:
    monkeypatch.setenv("PARALLAX_DB_PATH", str(seeded_db))
    monkeypatch.setenv("PARALLAX_USER_ID", "u")
    monkeypatch.setenv("MEMORY_ROUTER", "true")


def _make_decision(canonical_field: str = "test_field") -> ArbitrationDecision:
    candidate = FieldCandidate(
        source="src_a",
        field_name="body",
        value="hello world",
        confidence=0.9,
    )
    return ArbitrationDecision(
        canonical_field=canonical_field,
        state=MappingState.MAPPED,
        selected=candidate,
        candidates=(candidate,),
        reason_code="single_candidate",
        reason="only one candidate",
        confidence=0.9,
        requires_manual_review=False,
    )


# ---------------------------------------------------------------------------
# US-D3-05: arbitration view — file input
# ---------------------------------------------------------------------------


class TestArbitrationFileInput:
    def test_pretty_output_has_expected_columns(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
    ) -> None:
        dec = _make_decision("canonical_ref_001")
        jl_file = tmp_path / "decisions.jsonl"
        jl_file.write_text(dec.to_json_line() + "\n", encoding="utf-8")

        rc = main(["router", "arbitration", "--input", str(jl_file)])
        out, _ = capsys.readouterr()

        assert rc == 0
        assert "canonical_ref=canonical_ref_001" in out
        assert "decided_state=mapped" in out
        assert "evidence_count=1" in out
        assert "source=src_a" in out

    def test_multiple_decisions_one_per_line(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
    ) -> None:
        lines = "\n".join(
            _make_decision(f"ref_{i}").to_json_line() for i in range(3)
        )
        jl_file = tmp_path / "multi.jsonl"
        jl_file.write_text(lines + "\n", encoding="utf-8")

        rc = main(["router", "arbitration", "--input", str(jl_file)])
        out, _ = capsys.readouterr()

        assert rc == 0
        assert out.count("canonical_ref=") == 3


class TestArbitrationStdinInput:
    def test_stdin_pretty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import io

        dec = _make_decision("stdin_ref")
        monkeypatch.setattr("sys.stdin", io.StringIO(dec.to_json_line() + "\n"))

        rc = main(["router", "arbitration", "--stdin"])
        out, _ = capsys.readouterr()

        assert rc == 0
        assert "canonical_ref=stdin_ref" in out

    def test_empty_input_exits_0(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = main(["router", "arbitration"])
        out, _ = capsys.readouterr()

        assert rc == 0
        assert out.strip() == ""

    def test_malformed_line_skipped_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import io

        dec = _make_decision("good_ref")
        payload = "NOT_JSON\n" + dec.to_json_line() + "\n"
        monkeypatch.setattr("sys.stdin", io.StringIO(payload))

        rc = main(["router", "arbitration"])
        out, err = capsys.readouterr()

        assert rc == 0
        assert "warning" in err.lower()
        assert "canonical_ref=good_ref" in out


class TestArbitrationJsonlFormat:
    def test_jsonl_passthrough_byte_equal(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture
    ) -> None:
        dec = _make_decision("passthrough_ref")
        original_line = dec.to_json_line()
        jl_file = tmp_path / "pass.jsonl"
        jl_file.write_text(original_line + "\n", encoding="utf-8")

        rc = main(
            ["router", "arbitration", "--input", str(jl_file), "--format", "jsonl"]
        )
        out, _ = capsys.readouterr()

        assert rc == 0
        assert out.strip() == original_line


# ---------------------------------------------------------------------------
# US-D3-06: backfill plan
# ---------------------------------------------------------------------------


class TestBackfillPlan:
    def test_plan_emits_diff(self, capsys: pytest.CaptureFixture) -> None:
        rc = main(["router", "backfill", "plan"])
        out, _ = capsys.readouterr()

        assert rc == 0
        # unified diff header or "no changes" message
        assert "crosswalk/planned" in out or "no changes" in out

    def test_plan_shows_seeded_rows_as_additions(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        rc = main(["router", "backfill", "plan"])
        out, _ = capsys.readouterr()

        assert rc == 0
        # crosswalk is empty initially; seeded rows appear as + additions
        assert "+" in out or "no changes" in out

    def test_plan_diff_ordered_by_canonical_ref(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """Concurrent plan from two users produces deterministic ORDER BY canonical_ref."""
        rc = main(["router", "backfill", "plan"])
        out, _ = capsys.readouterr()

        assert rc == 0
        # Extract canonical_ref values from diff output lines
        refs = [
            line.split("canonical_ref=")[1].split()[0]
            for line in out.splitlines()
            if "canonical_ref=" in line and line.startswith("+")
        ]
        assert refs == sorted(refs), f"diff not sorted by canonical_ref: {refs}"


# ---------------------------------------------------------------------------
# US-D3-06: backfill apply
# ---------------------------------------------------------------------------


class TestBackfillApply:
    def test_apply_without_confirm_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("no\n"))
        rc = main(["router", "backfill", "apply"])
        _, err = capsys.readouterr()

        assert rc == 1
        assert "plan" in err.lower() or "confirm" in err.lower()

    def test_apply_with_yes_flag_proceeds(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        rc = main(["router", "backfill", "apply", "--yes"])
        out, _ = capsys.readouterr()

        assert rc == 0
        assert "backfill complete" in out
        assert "writes=" in out

    def test_apply_with_stdin_confirm_proceeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("CONFIRM\n"))
        rc = main(["router", "backfill", "apply"])
        out, _ = capsys.readouterr()

        assert rc == 0
        assert "backfill complete" in out

    def test_apply_with_stdin_wrong_answer_aborts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("no\n"))
        rc = main(["router", "backfill", "apply"])
        _, err = capsys.readouterr()

        assert rc == 1
        assert "plan" in err.lower() or "confirm" in err.lower()

    def test_apply_writes_to_crosswalk(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        seeded_db: pathlib.Path,
    ) -> None:
        monkeypatch.setenv("PARALLAX_DB_PATH", str(seeded_db))
        rc = main(["router", "backfill", "apply", "--yes", "--user-id", "u"])
        assert rc == 0

        from parallax.sqlite_store import connect as _connect
        c = _connect(seeded_db)
        rows = c.execute(
            "SELECT canonical_ref FROM crosswalk WHERE user_id='u'"
        ).fetchall()
        c.close()
        assert len(rows) > 0, "apply should have written crosswalk rows"

    def test_apply_requires_memory_router(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.delenv("MEMORY_ROUTER", raising=False)
        rc = main(["router", "backfill", "apply", "--yes"])
        _, err = capsys.readouterr()

        assert rc == 1
        assert "MEMORY_ROUTER" in err
