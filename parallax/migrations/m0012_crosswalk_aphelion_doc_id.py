"""Migration 0012 — rename ``crosswalk.dpkg_doc_id`` to ``aphelion_doc_id``.

Part of the DPKG → Aphelion rebrand (Aphelion v0.4.0 shipped 2026-04-24).
The crosswalk table introduced in m0011 carried the old ``dpkg_doc_id``
column name; this migration renames it in place so Parallax events can
round-trip against Aphelion packages under the new identifier.

SQLite ``ALTER TABLE ... RENAME COLUMN`` is supported since SQLite 3.25.
Parallax targets Python 3.10+, which bundles SQLite ≥ 3.37.
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    "ALTER TABLE crosswalk RENAME COLUMN dpkg_doc_id TO aphelion_doc_id",
]


def _require_rename_column_support() -> None:
    """Fail fast with a clear message on SQLite < 3.25 (no RENAME COLUMN).

    CPython 3.10+ bundles SQLite >= 3.37, so the realistic blast radius is
    non-CPython runtimes or Linux containers shipping an older libsqlite3
    (e.g., minimal Alpine). Surfacing a named RuntimeError here is strictly
    clearer than the cryptic ``OperationalError: near "COLUMN"`` that SQLite
    would otherwise raise mid-transaction.
    """
    if sqlite3.sqlite_version_info < (3, 25, 0):
        raise RuntimeError(
            "m0012 requires SQLite >= 3.25 for ALTER TABLE RENAME COLUMN; "
            f"runtime has {sqlite3.sqlite_version}. Upgrade your Python / "
            "libsqlite3, or stay pinned at schema version 11."
        )


def up(conn: sqlite3.Connection) -> None:
    _require_rename_column_support()
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    _require_rename_column_support()
    conn.execute(
        "ALTER TABLE crosswalk RENAME COLUMN aphelion_doc_id TO dpkg_doc_id"
    )
