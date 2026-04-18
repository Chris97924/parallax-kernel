"""Schema migration framework for Parallax.

Each migration is an ``up()`` / ``down()`` pair keyed by an integer version.
``schema_migrations`` records which versions have been applied so the same
migration is never run twice.

Atomicity (v0.1.5+):
    Each migration's ``up()`` runs inside an explicit
    ``BEGIN IMMEDIATE`` ... ``COMMIT`` block together with the matching
    ``schema_migrations`` insert. They succeed or fail together; on any
    exception the entire transaction is rolled back, so a partially-applied
    DDL pass is never recorded as applied. Migration ``up()`` functions
    therefore MUST issue individual ``conn.execute(stmt)`` calls and MUST
    NOT call ``conn.executescript`` (which would issue an implicit COMMIT
    and break this guarantee).

Public surface:

    Migration              -- frozen dataclass (version, name, up, down)
    MIGRATIONS             -- ordered list[Migration] (the registry)
    migrate_to_latest      -- apply all pending migrations atomically
    migrate_down_to        -- rollback to a target version (exclusive)
    applied_versions       -- read-only set[int] of applied versions
    pending                -- list[Migration] still to apply
"""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from parallax.migrations import (
    m0001_initial_schema,
    m0002_events_append_only,
    m0003_claim_metadata,
    m0004_events_user_time_index,
    m0005_claim_metadata_fk,
    m0006_events_session_id,
)
from parallax.sqlite_store import now_iso

__all__ = [
    "Migration",
    "MIGRATIONS",
    "migrate_to_latest",
    "migrate_down_to",
    "applied_versions",
    "pending",
    "ensure_schema_migrations_table",
]


@dataclasses.dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: Callable[[sqlite3.Connection], None]
    down: Callable[[sqlite3.Connection], None]


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="initial_schema",
        up=m0001_initial_schema.up,
        down=m0001_initial_schema.down,
    ),
    Migration(
        version=2,
        name="events_append_only",
        up=m0002_events_append_only.up,
        down=m0002_events_append_only.down,
    ),
    Migration(
        version=3,
        name="claim_metadata",
        up=m0003_claim_metadata.up,
        down=m0003_claim_metadata.down,
    ),
    Migration(
        version=4,
        name="events_user_time_index",
        up=m0004_events_user_time_index.up,
        down=m0004_events_user_time_index.down,
    ),
    Migration(
        version=5,
        name="claim_metadata_fk",
        up=m0005_claim_metadata_fk.up,
        down=m0005_claim_metadata_fk.down,
    ),
    Migration(
        version=6,
        name="events_session_id",
        up=m0006_events_session_id.up,
        down=m0006_events_session_id.down,
    ),
]


def ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the ``schema_migrations`` ledger if missing."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.commit()


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions already recorded as applied."""
    ensure_schema_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(r[0]) for r in rows}


def pending(conn: sqlite3.Connection) -> list[Migration]:
    """Return the unapplied migrations in ascending version order."""
    done = applied_versions(conn)
    return [m for m in sorted(MIGRATIONS, key=lambda m: m.version) if m.version not in done]


@contextmanager
def _manual_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block under an explicit BEGIN IMMEDIATE / COMMIT.

    Disables Python sqlite3's implicit transaction management for the
    duration so ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` are the
    only transaction boundaries in play. The previous isolation_level is
    restored on exit even if the block raises.
    """
    prev = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # No transaction active (already rolled back by SQLite on error).
            pass
        raise
    finally:
        conn.isolation_level = prev


def migrate_to_latest(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in version order, atomically.

    Returns the list of versions newly applied. Each migration's ``up()``
    runs inside an explicit ``BEGIN IMMEDIATE`` ... ``COMMIT`` together
    with the matching ``schema_migrations`` insert. A failure inside
    ``up()`` (or in the ledger insert) rolls the entire transaction back
    so the migration is neither half-applied nor recorded.
    """
    ensure_schema_migrations_table(conn)
    newly_applied: list[int] = []
    for mig in pending(conn):
        with _manual_tx(conn):
            mig.up(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (mig.version, mig.name, now_iso()),
            )
        newly_applied.append(mig.version)
    return newly_applied


def migrate_down_to(conn: sqlite3.Connection, target_version: int) -> list[int]:
    """Roll back applied migrations whose version > ``target_version``.

    Iterates in descending version order. Each ``down()`` + ledger row
    delete runs in the same explicit transaction as ``migrate_to_latest``
    uses for ``up()``: both succeed or both roll back.
    """
    ensure_schema_migrations_table(conn)
    done = applied_versions(conn)
    by_version = {m.version: m for m in MIGRATIONS}
    reverted: list[int] = []
    for v in sorted((v for v in done if v > target_version), reverse=True):
        mig = by_version.get(v)
        if mig is None:
            raise RuntimeError(
                f"schema_migrations references unknown version {v}; "
                f"refusing to roll back without a registered Migration"
            )
        with _manual_tx(conn):
            mig.down(conn)
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (v,))
        reverted.append(v)
    return reverted
