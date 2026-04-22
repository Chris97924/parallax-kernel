"""Tests for migration m0010 — memory_cards table schema."""

from __future__ import annotations

import sqlite3

import pytest

from parallax.migrations import (
    migrate_down_to,
    migrate_to_latest,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


class TestMemoryCardSchema:
    def test_migrate_to_latest_creates_memory_cards(
        self, conn: sqlite3.Connection
    ) -> None:
        migrate_to_latest(conn)

        # Table exists in sqlite_master
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_cards'"
        ).fetchone()
        assert row is not None, "memory_cards table not found"

        # Exact column names present
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_cards)").fetchall()}
        assert cols == {
            "id",
            "user_id",
            "category",
            "name",
            "filename",
            "description",
            "body",
            "created_at",
            "updated_at",
        }

    def test_migrate_down_to_9_drops_table(
        self, conn: sqlite3.Connection
    ) -> None:
        migrate_to_latest(conn)
        migrate_down_to(conn, 9)

        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_cards'"
        ).fetchone()
        assert row is None, "memory_cards table should be gone after down to v9"

    def test_round_trip_up_down_up(self, conn: sqlite3.Connection) -> None:
        migrate_to_latest(conn)
        migrate_down_to(conn, 9)

        # Table gone
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_cards'"
        ).fetchone()
        assert row is None

        # Up again
        migrate_to_latest(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_cards'"
        ).fetchone()
        assert row is not None, "memory_cards table should exist after second up"

    def test_category_check_constraint(self, conn: sqlite3.Connection) -> None:
        migrate_to_latest(conn)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_cards"
                "(id, user_id, category, name, filename, description, body, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "c1",
                    "u1",
                    "bogus",
                    "Test Card",
                    "test.md",
                    "A test card",
                    "body text",
                    "2026-04-22T00:00:00Z",
                    "2026-04-22T00:00:00Z",
                ),
            )
            conn.commit()

    def test_unique_user_filename(self, conn: sqlite3.Connection) -> None:
        migrate_to_latest(conn)

        conn.execute(
            "INSERT INTO memory_cards"
            "(id, user_id, category, name, filename, description, body, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "c1",
                "u1",
                "user",
                "Card One",
                "card.md",
                "first",
                "body",
                "2026-04-22T00:00:00Z",
                "2026-04-22T00:00:00Z",
            ),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_cards"
                "(id, user_id, category, name, filename, description, body, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "c2",
                    "u1",
                    "project",
                    "Card Two",
                    "card.md",
                    "second",
                    "body2",
                    "2026-04-22T00:00:00Z",
                    "2026-04-22T00:00:00Z",
                ),
            )
            conn.commit()
