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
#
# NOTE: The ``sha256(...)`` call in the UPDATE literal is illustrative
# only — SQLite has no native ``sha256`` function. The real rehash is
# performed row-by-row in Python by ``up()`` below, using
# ``parallax.hashing.content_hash``. Only ``migration_plan`` ever sees
# this string, and only to classify the statement as "UPDATE claims"
# for impact estimation.
STATEMENTS: list[str] = [
    "DROP INDEX IF EXISTS uniq_claims_content",
    "UPDATE claims SET content_hash = sha256(subject||predicate||object||source_id||user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_content "
    "ON claims(content_hash, source_id, user_id)",
]


def up(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS uniq_claims_content")
    # Use sqlite3.Row factory for named access; resilient to column
    # reordering and easier to audit than positional ``r[1]..r[5]``.
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT claim_id, subject, predicate, object, source_id, user_id "
            "FROM claims"
        ).fetchall()
    finally:
        conn.row_factory = prev_factory
    for r in rows:
        new_hash = content_hash(
            r["subject"], r["predicate"], r["object"], r["source_id"], r["user_id"]
        )
        conn.execute(
            "UPDATE claims SET content_hash = ? WHERE claim_id = ?",
            (new_hash, r["claim_id"]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_content "
        "ON claims(content_hash, source_id, user_id)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS uniq_claims_content")
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT claim_id, subject, predicate, object, source_id FROM claims"
        ).fetchall()
    finally:
        conn.row_factory = prev_factory
    for r in rows:
        old_hash = content_hash(
            r["subject"], r["predicate"], r["object"], r["source_id"]
        )
        conn.execute(
            "UPDATE claims SET content_hash = ? WHERE claim_id = ?",
            (old_hash, r["claim_id"]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_claims_content "
        "ON claims(content_hash, source_id)"
    )
