"""Migration 0005 — claim_metadata FK semantics tightening.

Recreates ``claim_metadata`` via the SQLite standard table-swap pattern
(CREATE _new, COPY, DROP old, RENAME) to add:

* ``ON DELETE SET NULL`` on ``superseded_by`` — a deleted successor no
  longer blocks deletion of its predecessor metadata row; the pointer
  simply becomes NULL.
* ``CHECK (claim_id != superseded_by)`` — prevents a row from declaring
  itself its own successor (tautological self-supersession).

Data is preserved. Existing rows that happen to have
``claim_id = superseded_by`` would violate the new CHECK and abort the
migration; this is intentional — such rows are corrupt by definition.
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS idx_claim_metadata_superseded_by",
    """
    CREATE TABLE claim_metadata_new (
        claim_id        TEXT PRIMARY KEY REFERENCES claims(claim_id),
        reaffirm_count  INTEGER NOT NULL DEFAULT 0,
        last_seen_at    TIMESTAMP NOT NULL,
        superseded_by   TEXT REFERENCES claims(claim_id) ON DELETE SET NULL,
        superseded_at   TIMESTAMP,
        created_at      TIMESTAMP NOT NULL,
        updated_at      TIMESTAMP NOT NULL,
        CHECK (superseded_by IS NULL OR claim_id != superseded_by)
    )
    """,
    """
    INSERT INTO claim_metadata_new (
        claim_id, reaffirm_count, last_seen_at, superseded_by,
        superseded_at, created_at, updated_at
    )
    SELECT claim_id, reaffirm_count, last_seen_at, superseded_by,
           superseded_at, created_at, updated_at
    FROM claim_metadata
    """,
    "DROP TABLE claim_metadata",
    "ALTER TABLE claim_metadata_new RENAME TO claim_metadata",
    "CREATE INDEX idx_claim_metadata_superseded_by ON claim_metadata(superseded_by)",
]

DOWN_STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS idx_claim_metadata_superseded_by",
    """
    CREATE TABLE claim_metadata_old (
        claim_id        TEXT PRIMARY KEY REFERENCES claims(claim_id),
        reaffirm_count  INTEGER NOT NULL DEFAULT 0,
        last_seen_at    TIMESTAMP NOT NULL,
        superseded_by   TEXT REFERENCES claims(claim_id),
        superseded_at   TIMESTAMP,
        created_at      TIMESTAMP NOT NULL,
        updated_at      TIMESTAMP NOT NULL
    )
    """,
    """
    INSERT INTO claim_metadata_old (
        claim_id, reaffirm_count, last_seen_at, superseded_by,
        superseded_at, created_at, updated_at
    )
    SELECT claim_id, reaffirm_count, last_seen_at, superseded_by,
           superseded_at, created_at, updated_at
    FROM claim_metadata
    """,
    "DROP TABLE claim_metadata",
    "ALTER TABLE claim_metadata_old RENAME TO claim_metadata",
    "CREATE INDEX idx_claim_metadata_superseded_by ON claim_metadata(superseded_by)",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    for stmt in DOWN_STATEMENTS:
        conn.execute(stmt)
