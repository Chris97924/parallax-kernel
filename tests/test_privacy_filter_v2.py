"""Tests for S7 — body-only regex-pattern privacy filter (body_looks_like_secret)."""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.memory_md import body_looks_like_secret, ingest_memory_md
from parallax.migrations import migrate_to_latest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    migrate_to_latest(c)
    yield c
    c.close()


def _write_companion(
    tmp_path: pathlib.Path,
    filename: str,
    name: str,
    description: str,
    ftype: str,
    body: str,
) -> None:
    p = tmp_path / filename
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {ftype}\n---\n\n{body}",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. Positive cases — real secret patterns must match
# ---------------------------------------------------------------------------


def test_pattern_matches_real_secrets() -> None:
    assert body_looks_like_secret("password: hunter2xyz")
    assert body_looks_like_secret("PASSWORD=abc12345")
    assert body_looks_like_secret("token: ghp_abc123def456")
    assert body_looks_like_secret("api_key = sk-xyzabc789")
    assert body_looks_like_secret("api-key: abc123def456")
    assert body_looks_like_secret("credential: admin_secret_xyz")
    assert body_looks_like_secret("private_key = -----BEGIN_something")
    assert body_looks_like_secret(".env=secret_value_123")


# ---------------------------------------------------------------------------
# 2. Negative cases — prose mentions must NOT match
# ---------------------------------------------------------------------------


def test_pattern_rejects_prose_mentions() -> None:
    assert not body_looks_like_secret("token 月度 cap lesson")
    assert not body_looks_like_secret("API key rotation policy doc")
    assert not body_looks_like_secret("secret santa gift list")
    assert not body_looks_like_secret("password reset flow")
    assert not body_looks_like_secret("credential store documentation")
    assert not body_looks_like_secret(".env file should not be committed")
    assert not body_looks_like_secret("Hub-and-spoke HTTP server")


# ---------------------------------------------------------------------------
# 3. Body-only scope: name containing 'secret' must NOT trigger filter
# ---------------------------------------------------------------------------


def test_body_only_scope_ingest(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """name='secret_admin_xyz' with benign body — card must be INSERTED."""
    md = tmp_path / "MEMORY.md"
    md.write_text(
        "# User\n- [Secret Admin](secret_admin.md) — admin card\n",
        encoding="utf-8",
    )
    _write_companion(
        tmp_path,
        "secret_admin.md",
        "secret_admin_xyz",  # 'secret' in name — body-only scope means not filtered
        "admin card",
        "user",
        "benign content",
    )

    report = ingest_memory_md(conn, memory_md_path=md, user_id="test_user")
    assert "secret_admin.md" not in report.skipped_privacy
    assert report.cards_inserted == 1


# ---------------------------------------------------------------------------
# 4. Body with real secret must still be blocked
# ---------------------------------------------------------------------------


def test_body_with_real_secret_still_blocked(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """Body containing a real key=value secret must end up in skipped_privacy."""
    md = tmp_path / "MEMORY.md"
    md.write_text(
        "# Reference\n- [Creds](creds.md) — credentials file\n",
        encoding="utf-8",
    )
    _write_companion(
        tmp_path,
        "creds.md",
        "Creds",
        "credentials file",
        "reference",
        "token: ghp_abc123def456",  # real GitHub PAT pattern
    )

    report = ingest_memory_md(conn, memory_md_path=md, user_id="test_user")
    assert "creds.md" in report.skipped_privacy
    assert report.cards_inserted == 0


# ---------------------------------------------------------------------------
# 5. Real Chris MEMORY.md — zero false positives after S7 fix
# ---------------------------------------------------------------------------

_REAL_MEMORY_MD = pathlib.Path(
    "C:/Users/user/.claude/projects/C--Users-user/memory/MEMORY.md"
)


@pytest.mark.skipif(
    not _REAL_MEMORY_MD.exists(),
    reason="Chris host only",
)
def test_real_chris_memory_md_no_false_positives(
    conn: sqlite3.Connection,
) -> None:
    """The 2 previously-blocked cards (longmemeval + v06_architecture) now ingest."""
    report = ingest_memory_md(
        conn, memory_md_path=_REAL_MEMORY_MD, user_id="chris"
    )
    assert len(report.skipped_privacy) == 0, (
        f"Expected 0 privacy-skipped after S7 fix, got "
        f"{len(report.skipped_privacy)}: {report.skipped_privacy}"
    )
    assert report.cards_inserted + report.cards_updated >= 10
