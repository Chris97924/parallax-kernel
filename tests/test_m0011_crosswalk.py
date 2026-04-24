"""Tests for migration m0011 — crosswalk table schema."""

from __future__ import annotations

import pathlib
import sqlite3

from parallax.migrations import migrate_down_to, migrate_to_latest
from parallax.sqlite_store import connect


def _fresh_conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "m0011.db"
    return connect(db)


def test_migrate_to_latest_creates_crosswalk(tmp_path: pathlib.Path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        migrate_to_latest(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='crosswalk'"
        ).fetchone()
        assert row is not None

        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(crosswalk)").fetchall()
        }
        assert {
            "user_id",
            "canonical_ref",
            "parallax_target_kind",
            "parallax_target_id",
            "query_type",
            "state",
            "content_hash",
            "source_id",
            "vault_path",
            "dpkg_doc_id",
            "last_event_id_seen",
            "last_embedded_at",
            "created_at",
            "updated_at",
        }.issubset(cols)
    finally:
        conn.close()


def test_down_to_v10_drops_crosswalk(tmp_path: pathlib.Path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        migrate_to_latest(conn)
        reverted = migrate_down_to(conn, target_version=10)
        assert 11 in reverted
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='crosswalk'"
        ).fetchone()
        assert row is None
    finally:
        conn.close()

