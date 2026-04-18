"""Migration 0003 — claim_metadata sidecar (Schema B).

Separates per-claim mutable metadata (reaffirm count, supersession pointer,
last-seen timestamp) from the immutable identity/dedup fields on the
``claims`` table. Lets reaffirm and state-change events update a small,
narrow row instead of touching the dedup-key surface.

NOTE: This migration's FK semantics are tightened by migration 0005
(``ON DELETE SET NULL`` + self-reference CHECK). Fresh DBs apply 0003 then
0005 in sequence; DBs that stopped at 0003 upgrade cleanly to 0005 via the
table-swap pattern.

Schema:
    claim_metadata
      claim_id       PK -> claims.claim_id
      reaffirm_count INTEGER (default 0)
      last_seen_at   TIMESTAMP (last reaffirmed-at, defaults to claim.created_at)
      superseded_by  -> claims.claim_id (nullable; points at the replacement)
      superseded_at  TIMESTAMP (nullable)
      created_at     TIMESTAMP (sidecar row creation)
      updated_at     TIMESTAMP
"""

from __future__ import annotations

import sqlite3

STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS claim_metadata (
        claim_id        TEXT PRIMARY KEY REFERENCES claims(claim_id),
        reaffirm_count  INTEGER NOT NULL DEFAULT 0,
        last_seen_at    TIMESTAMP NOT NULL,
        superseded_by   TEXT REFERENCES claims(claim_id),
        superseded_at   TIMESTAMP,
        created_at      TIMESTAMP NOT NULL,
        updated_at      TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_claim_metadata_superseded_by ON claim_metadata(superseded_by)",
]

DOWN_STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS idx_claim_metadata_superseded_by",
    "DROP TABLE IF EXISTS claim_metadata",
]


def up(conn: sqlite3.Connection) -> None:
    for stmt in STATEMENTS:
        conn.execute(stmt)


def down(conn: sqlite3.Connection) -> None:
    for stmt in DOWN_STATEMENTS:
        conn.execute(stmt)
