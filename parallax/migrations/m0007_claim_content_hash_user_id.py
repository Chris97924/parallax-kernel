"""Migration 0007 — claim content_hash scopes to user_id (ADR-005).

Rehashes every existing claim row with the new 5-part formula
``sha256(normalize(subject || predicate || object || source_id || user_id))``
and swaps the UNIQUE index from ``(content_hash, source_id)`` to
``(content_hash, source_id, user_id)``. See
``docs/adr/ADR-005-claim-content-hash-user-id-scope.md`` for the
rationale.

The rehash runs in Python because SQLite cannot reproduce
``parallax.hashing.content_hash`` in pure SQL. Each UPDATE is a single
``conn.execute`` call so the whole migration stays inside the
``BEGIN IMMEDIATE`` transaction opened by ``migrate_to_latest``.
"""

from __future__ import annotations

import sqlite3

from parallax.hashing import content_hash

# ``STATEMENTS`` is consumed by the static ``migration_plan`` for row
# impact estimation. The UPDATE sweep is represented as a single
# "UPDATE claims" entry because the Python loop issues N individual
# updates whose count equals the row count reported by the estimator.
STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS uniq_claims_content",
    "UPDATE claims SET content_hash = sha256(subject||predicate||object||source_id||user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_content ON claims(content_hash, source_id, user_id)",
]


def up(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS uniq_claims_content")
    rows = conn.execute(
        "SELECT claim_id, subject, predicate, object, source_id, user_id "
        "FROM claims"
    ).fetchall()
    for r in rows:
        new_hash = content_hash(r[1], r[2], r[3], r[4], r[5])
        conn.execute(
            "UPDATE claims SET content_hash = ? WHERE claim_id = ?",
            (new_hash, r[0]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_content "
        "ON claims(content_hash, source_id, user_id)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS uniq_claims_content")
    rows = conn.execute(
        "SELECT claim_id, subject, predicate, object, source_id FROM claims"
    ).fetchall()
    for r in rows:
        old_hash = content_hash(r[1], r[2], r[3], r[4])
        conn.execute(
            "UPDATE claims SET content_hash = ? WHERE claim_id = ?",
            (old_hash, r[0]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_content "
        "ON claims(content_hash, source_id)"
    )
