"""Migration 0009 — api_tokens table for multi-user auth.

Covers:
    * migrate_to_latest creates the ``api_tokens`` table + index and
      records version 9 in ``schema_migrations``.
    * Direct re-``up`` is a no-op thanks to ``IF NOT EXISTS`` guards;
      re-running ``migrate_to_latest`` against an already-applied DB is
      also a no-op (registry short-circuits).
    * ``down()`` drops the table and its supporting index cleanly.
    * ``migration_plan`` surfaces the m0009 step with the expected
      statements and a zero row-impact estimate on a fresh DB.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from parallax.migrations import (
    MIGRATIONS,
    _manual_tx,
    ensure_schema_migrations_table,
    m0009_api_tokens,
    migrate_down_to,
    migrate_to_latest,
    migration_plan,
)
from parallax.sqlite_store import connect, now_iso


@pytest.fixture()
def fresh_db(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "m0009.db"
    c = connect(db)
    migrate_to_latest(c)
    return c


@pytest.fixture()
def db_at_v8(tmp_path: pathlib.Path) -> sqlite3.Connection:
    """Seed a DB at migration version 8 (pre-m0009)."""
    db = tmp_path / "pre_m0009.db"
    c = connect(db)
    ensure_schema_migrations_table(c)
    for mig in sorted(MIGRATIONS, key=lambda m: m.version):
        if mig.version > 8:
            continue
        with _manual_tx(c):
            mig.up(c)
            c.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (mig.version, mig.name, now_iso()),
            )
    return c


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


class TestUp:
    def test_creates_table_and_index(self, fresh_db: sqlite3.Connection) -> None:
        assert _table_exists(fresh_db, "api_tokens")
        assert _index_exists(fresh_db, "idx_api_tokens_user_id")

    def test_columns_present(self, fresh_db: sqlite3.Connection) -> None:
        cols = {
            r[1] for r in fresh_db.execute("PRAGMA table_info(api_tokens)").fetchall()
        }
        assert cols == {"token_hash", "user_id", "created_at", "revoked_at", "label"}

    def test_registered_at_version_9(self, fresh_db: sqlite3.Connection) -> None:
        rows = fresh_db.execute(
            "SELECT version, name FROM schema_migrations WHERE version = 9"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "api_tokens"


class TestIdempotency:
    def test_migrate_to_latest_short_circuits(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        applied = migrate_to_latest(fresh_db)
        # Everything already applied → no new work.
        assert applied == []

    def test_direct_up_is_noop_on_fresh_db(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        # Re-invoking ``up`` against a DB where the table already exists
        # must not raise (the migration uses IF NOT EXISTS guards).
        m0009_api_tokens.up(fresh_db)
        assert _table_exists(fresh_db, "api_tokens")
        assert _index_exists(fresh_db, "idx_api_tokens_user_id")


class TestDown:
    def test_drops_table_and_index(self, fresh_db: sqlite3.Connection) -> None:
        reverted = migrate_down_to(fresh_db, target_version=8)
        assert 9 in reverted
        assert not _table_exists(fresh_db, "api_tokens")
        assert not _index_exists(fresh_db, "idx_api_tokens_user_id")

    def test_down_is_idempotent_via_if_exists(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        # Calling down() directly a second time must not raise.
        m0009_api_tokens.down(fresh_db)
        m0009_api_tokens.down(fresh_db)
        assert not _table_exists(fresh_db, "api_tokens")


class TestMigrationPlan:
    def test_plan_lists_m0009_when_pending(
        self, db_at_v8: sqlite3.Connection
    ) -> None:
        plan = migration_plan(db_at_v8)
        pending_versions = [step.version for step in plan.pending]
        assert 9 in pending_versions
        step = next(s for s in plan.pending if s.version == 9)
        assert step.name == "api_tokens"
        # STATEMENTS should reference the api_tokens table.
        assert any("api_tokens" in s for s in step.statements)
        # Fresh DB — no existing rows yet.
        assert step.row_impact_estimates.get("api_tokens", 0) == 0

    def test_plan_empty_when_applied(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        plan = migration_plan(fresh_db)
        assert 9 in plan.applied
        assert all(s.version != 9 for s in plan.pending)


class TestRoundTrip:
    def test_insert_and_query(self, fresh_db: sqlite3.Connection) -> None:
        fresh_db.execute(
            "INSERT INTO api_tokens(token_hash, user_id, created_at, "
            "revoked_at, label) VALUES (?, ?, ?, NULL, ?)",
            ("h" * 64, "alice", now_iso(), "smoke"),
        )
        fresh_db.commit()
        row = fresh_db.execute(
            "SELECT user_id, label FROM api_tokens WHERE token_hash = ?",
            ("h" * 64,),
        ).fetchone()
        assert row["user_id"] == "alice"
        assert row["label"] == "smoke"
