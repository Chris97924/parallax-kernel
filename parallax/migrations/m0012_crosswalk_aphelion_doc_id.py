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


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    conn.execute(
        "ALTER TABLE crosswalk RENAME COLUMN aphelion_doc_id TO dpkg_doc_id"
    )
