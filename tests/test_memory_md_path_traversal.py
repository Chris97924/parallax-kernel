"""Path-traversal guard tests for ingest_memory_md."""

from __future__ import annotations

import sqlite3
import sys
import textwrap

import pytest

from parallax.memory_md import ingest_memory_md
from parallax.migrations import migrate_to_latest


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    migrate_to_latest(c)
    yield c
    c.close()


def _write_companion(
    tmp_path,
    filename: str,
    name: str = "Test Card",
    description: str = "test desc",
    ftype: str = "user",
    body: str = "body text",
) -> None:
    p = tmp_path / filename
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {ftype}\n---\n\n{body}",
        encoding="utf-8",
    )


def _make_memory_md(tmp_path, filename: str) -> object:
    """Write a MEMORY.md referencing *filename* and return its path."""
    md = tmp_path / "MEMORY.md"
    md.write_text(
        textwrap.dedent(
            f"""\
            # User
            - [Escape Card]({filename}) — description
            """
        ),
        encoding="utf-8",
    )
    return md


def test_filename_with_parent_escape_rejected(
    conn: sqlite3.Connection, tmp_path
) -> None:
    """A filename like '../escape.md' must land in skipped_malformed."""
    # Create the file outside tmp_path so it actually exists (tests the guard
    # fires before the exists() probe, but having it exist makes the test
    # definitive regardless of order).
    escape_file = tmp_path.parent / "escape.md"
    escape_file.write_text(
        "---\nname: Escape\ndescription: d\ntype: user\n---\n\nbody",
        encoding="utf-8",
    )
    try:
        md = _make_memory_md(tmp_path, "../escape.md")
        report = ingest_memory_md(conn, memory_md_path=md, user_id="u1")
        assert "../escape.md" in report.skipped_malformed, (
            f"Expected '../escape.md' in skipped_malformed, got {report}"
        )
        assert report.cards_inserted == 0
        assert "../escape.md" not in report.skipped_missing_companion
    finally:
        escape_file.unlink(missing_ok=True)


def test_absolute_posix_path_filename_rejected(
    conn: sqlite3.Connection, tmp_path
) -> None:
    """An absolute POSIX path like '/etc/passwd' must land in skipped_malformed."""
    md = _make_memory_md(tmp_path, "/etc/passwd")
    report = ingest_memory_md(conn, memory_md_path=md, user_id="u2")
    assert "/etc/passwd" in report.skipped_malformed, (
        f"Expected '/etc/passwd' in skipped_malformed, got {report}"
    )
    assert report.cards_inserted == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows absolute path test")
def test_absolute_windows_path_filename_rejected(
    conn: sqlite3.Connection, tmp_path
) -> None:
    """An absolute Windows path like 'C:/windows/system32/notepad.exe' must be rejected."""
    md = _make_memory_md(tmp_path, "C:/windows/system32/notepad.exe")
    report = ingest_memory_md(conn, memory_md_path=md, user_id="u3")
    assert "C:/windows/system32/notepad.exe" in report.skipped_malformed, (
        f"Expected Windows path in skipped_malformed, got {report}"
    )
    assert report.cards_inserted == 0


def test_benign_relative_filename_still_ingests(
    conn: sqlite3.Connection, tmp_path
) -> None:
    """A normal filename in the same directory must still be ingested."""
    _write_companion(tmp_path, "user_ok.md")
    md = _make_memory_md(tmp_path, "user_ok.md")
    report = ingest_memory_md(conn, memory_md_path=md, user_id="u4")
    assert report.cards_inserted == 1, (
        f"Expected 1 card inserted, got {report}"
    )
    assert report.skipped_malformed == ()
    assert report.skipped_missing_companion == ()
