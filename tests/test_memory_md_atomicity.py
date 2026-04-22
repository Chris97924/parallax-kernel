"""Atomic-transaction tests for ingest_memory_md."""

from __future__ import annotations

import sqlite3
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
    name: str = "Card",
    description: str = "desc",
    ftype: str = "user",
    body: str = "body",
) -> None:
    p = tmp_path / filename
    p.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {ftype}\n---\n\n{body}",
        encoding="utf-8",
    )


def _make_3entry_dir(tmp_path):
    """Create a MEMORY.md with 3 companions and return the MEMORY.md path."""
    md = tmp_path / "MEMORY.md"
    md.write_text(
        textwrap.dedent(
            """\
            # User
            - [Card A](card_a.md) — description A

            # Projects (Active)
            - [Card B](card_b.md) — description B

            # Feedback
            - [Card C](card_c.md) — description C
            """
        ),
        encoding="utf-8",
    )
    for fname in ("card_a.md", "card_b.md", "card_c.md"):
        _write_companion(tmp_path, fname)
    return md


class _SpyConnection:
    """Thin wrapper around sqlite3.Connection that intercepts execute() calls."""

    def __init__(self, real_conn: sqlite3.Connection) -> None:
        self._conn = real_conn
        self.sql_log: list[str] = []
        self._upsert_count = 0
        self.fail_on_upsert: int | None = None  # raise on this UPSERT ordinal (1-based)

    def execute(self, sql: str, *args, **kwargs):
        self.sql_log.append(sql.strip())
        if "INSERT INTO memory_cards" in sql:
            self._upsert_count += 1
            if (
                self.fail_on_upsert is not None
                and self._upsert_count == self.fail_on_upsert
            ):
                raise sqlite3.OperationalError("simulated mid-run crash")
        return self._conn.execute(sql, *args, **kwargs)

    # Delegate everything else to the real connection
    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def test_ingest_is_atomic_on_failure(tmp_path) -> None:
    """If an error occurs mid-loop, the entire transaction rolls back (zero rows)."""
    real_conn = sqlite3.connect(":memory:")
    real_conn.execute("PRAGMA foreign_keys = ON")
    migrate_to_latest(real_conn)

    md = _make_3entry_dir(tmp_path)
    spy = _SpyConnection(real_conn)
    spy.fail_on_upsert = 2  # blow up on the 2nd INSERT INTO memory_cards

    with pytest.raises(sqlite3.OperationalError, match="simulated mid-run crash"):
        ingest_memory_md(spy, memory_md_path=md, user_id="atomic_user")  # type: ignore[arg-type]

    count = real_conn.execute(
        "SELECT COUNT(*) FROM memory_cards WHERE user_id = ?", ("atomic_user",)
    ).fetchone()[0]
    assert count == 0, f"Expected 0 rows after rollback, got {count}"
    real_conn.close()


def test_single_begin_commit_pair(tmp_path) -> None:
    """Exactly one BEGIN IMMEDIATE and one COMMIT for a 3-entry successful ingest."""
    real_conn = sqlite3.connect(":memory:")
    real_conn.execute("PRAGMA foreign_keys = ON")
    migrate_to_latest(real_conn)

    md = _make_3entry_dir(tmp_path)
    spy = _SpyConnection(real_conn)

    ingest_memory_md(spy, memory_md_path=md, user_id="tx_user")  # type: ignore[arg-type]

    begins = [s for s in spy.sql_log if s.upper().startswith("BEGIN")]
    commits = [s for s in spy.sql_log if s.upper() == "COMMIT"]
    assert len(begins) == 1, f"Expected 1 BEGIN, got {len(begins)}: {begins}"
    assert len(commits) == 1, f"Expected 1 COMMIT, got {len(commits)}: {commits}"
    real_conn.close()


def test_idempotency_preserved_under_atomic_tx(
    conn: sqlite3.Connection, tmp_path
) -> None:
    """Moving to single-commit must NOT break the idempotency contract."""
    md = _make_3entry_dir(tmp_path)

    report1 = ingest_memory_md(conn, memory_md_path=md, user_id="idem_user")
    assert report1.cards_inserted == 3
    assert report1.cards_updated == 0

    report2 = ingest_memory_md(conn, memory_md_path=md, user_id="idem_user")
    assert report2.cards_inserted == 0
    assert report2.cards_updated == 3
