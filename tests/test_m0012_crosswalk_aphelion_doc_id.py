"""Tests for migration m0012 — rename ``crosswalk.dpkg_doc_id`` to
``aphelion_doc_id`` (DPKG → Aphelion rebrand, Aphelion v0.4.0+).
"""

from __future__ import annotations

import pathlib
import sqlite3

from parallax.migrations import (
    migrate_down_to,
    migrate_to_latest,
    migration_plan,
)
from parallax.sqlite_store import connect


def _fresh_conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "m0012.db"
    return connect(db)


def _crosswalk_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(crosswalk)").fetchall()}


def test_m0012_renames_column_to_aphelion_doc_id(tmp_path: pathlib.Path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        migrate_to_latest(conn)
        cols = _crosswalk_columns(conn)
        assert "aphelion_doc_id" in cols
        assert "dpkg_doc_id" not in cols
    finally:
        conn.close()


def test_m0012_preserves_row_data(tmp_path: pathlib.Path) -> None:
    """Insert under the pre-rename column, apply m0012, value survives."""
    conn = _fresh_conn(tmp_path)
    try:
        migrate_to_latest(conn)
        # Roll back m0012 so the column is dpkg_doc_id again, then insert
        # a row under the pre-rename name and re-apply m0012.
        migrate_down_to(conn, target_version=11)
        assert "dpkg_doc_id" in _crosswalk_columns(conn)

        conn.execute(
            "INSERT INTO crosswalk ("
            "user_id, canonical_ref, parallax_target_kind, parallax_target_id, "
            "state, content_hash, dpkg_doc_id, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "u1",
                "claim:abc",
                "claim",
                "t1",
                "mapped",
                "h1",
                "aphe-doc-123",
                "2026-04-25T00:00:00Z",
                "2026-04-25T00:00:00Z",
            ),
        )
        conn.commit()

        # Sanity check: row is readable via the pre-rename column before re-up.
        # Guards against a regression where ``migrate_down_to(11)`` drops the
        # table — without this, the post-rename assertion below would pass
        # tautologically on a freshly rebuilt empty table.
        pre_row = conn.execute(
            "SELECT dpkg_doc_id FROM crosswalk "
            "WHERE user_id = ? AND canonical_ref = ?",
            ("u1", "claim:abc"),
        ).fetchone()
        assert pre_row is not None
        assert pre_row[0] == "aphe-doc-123"

        migrate_to_latest(conn)
        assert "aphelion_doc_id" in _crosswalk_columns(conn)

        row = conn.execute(
            "SELECT aphelion_doc_id FROM crosswalk "
            "WHERE user_id = ? AND canonical_ref = ?",
            ("u1", "claim:abc"),
        ).fetchone()
        assert row is not None
        assert row[0] == "aphe-doc-123"
    finally:
        conn.close()


def test_m0012_down_to_v11_restores_dpkg_doc_id(tmp_path: pathlib.Path) -> None:
    conn = _fresh_conn(tmp_path)
    try:
        migrate_to_latest(conn)
        reverted = migrate_down_to(conn, target_version=11)
        assert 12 in reverted
        cols = _crosswalk_columns(conn)
        assert "dpkg_doc_id" in cols
        assert "aphelion_doc_id" not in cols
    finally:
        conn.close()


def test_m0012_is_registered_in_migration_plan(tmp_path: pathlib.Path) -> None:
    """m0012 shows up in a fresh DB's pending plan before any migration runs."""
    conn = _fresh_conn(tmp_path)
    try:
        plan = migration_plan(conn)
        versions = {step.version for step in plan.pending}
        assert 12 in versions
        assert plan.target_version >= 12
    finally:
        conn.close()
