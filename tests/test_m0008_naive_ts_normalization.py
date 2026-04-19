"""Migration 0008 — corpus normalization of legacy timestamp strings.

Pins the v0.5.0-pre4 CRITICAL fix: every stored ISO-8601 timestamp must
end up in the exact 32-char ``now_iso()`` canonical form so that the
SQLite lex-compare in :func:`parallax.retrieve.by_timeline` can never
flip at a naive-ts same-second boundary.

Covers:
    * Seed a DB at migration version 7, write three shapes of stored
      timestamp, run ``migrate_to_latest``, assert every row ends
      32 chars long and survives a ``datetime.fromisoformat`` round-trip.
    * Idempotency: a second ``migrate_to_latest`` is a no-op (registry
      short-circuits); directly re-invoking ``m0008.up`` against the
      already-normalized corpus produces byte-identical columns.
    * Events append-only trigger survives: after the migration completes
      an app-level UPDATE on events still aborts with the append-only
      ``IntegrityError``.
    * naive-UTC inputs are normalized to ``+00:00``.
"""

from __future__ import annotations

import datetime as _dt
import pathlib
import sqlite3

import pytest

from parallax.migrations import (
    MIGRATIONS,
    _manual_tx,
    ensure_schema_migrations_table,
    m0008_normalize_naive_created_at,
    migrate_to_latest,
)
from parallax.sqlite_store import connect, now_iso

CANONICAL_LEN = 32


@pytest.fixture()
def db_at_v7(tmp_path: pathlib.Path) -> sqlite3.Connection:
    """Seed a database at migration version 7 (pre-m0008)."""
    db = tmp_path / "m0008.db"
    c = connect(db)
    ensure_schema_migrations_table(c)
    for mig in sorted(MIGRATIONS, key=lambda m: m.version):
        if mig.version > 7:
            continue
        with _manual_tx(c):
            mig.up(c)
            c.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) "
                "VALUES (?, ?, ?)",
                (mig.version, mig.name, now_iso()),
            )
    return c


def _seed_source(conn: sqlite3.Connection, source_id: str, ingested_at: str) -> None:
    conn.execute(
        """INSERT INTO sources(source_id, uri, kind, content_hash, user_id,
                               ingested_at, state)
           VALUES (?, 'file://x', 'note', 'h', 'u', ?, 'active')""",
        (source_id, ingested_at),
    )


def _seed_claim(
    conn: sqlite3.Connection,
    claim_id: str,
    source_id: str,
    created_at: str,
    updated_at: str,
) -> None:
    conn.execute(
        """INSERT INTO claims(claim_id, user_id, subject, predicate, object,
                              source_id, content_hash, confidence, state,
                              created_at, updated_at)
           VALUES (?, 'u', 's', 'p', 'o', ?, ?, NULL, 'auto', ?, ?)""",
        (claim_id, source_id, claim_id + "_h", created_at, updated_at),
    )


def _seed_event(conn: sqlite3.Connection, event_id: str, created_at: str) -> None:
    conn.execute(
        """INSERT INTO events(event_id, user_id, actor, event_type,
                              target_kind, target_id, payload_json,
                              approval_tier, created_at)
           VALUES (?, 'u', 'system', 'marker', NULL, NULL, '{}', NULL, ?)""",
        (event_id, created_at),
    )


def _all_column(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    return [r[0] for r in conn.execute(f"SELECT {column} FROM {table}").fetchall()]


class TestCanonicalShape:
    def test_naive_19_char_is_expanded(self, db_at_v7: sqlite3.Connection) -> None:
        _seed_source(db_at_v7, "s1", "2024-06-15T12:00:00")
        _seed_claim(db_at_v7, "c1", "s1", "2024-06-15T12:00:00", "2024-06-15T12:00:00")
        _seed_event(db_at_v7, "e1", "2024-06-15T12:00:00")
        db_at_v7.commit()

        migrate_to_latest(db_at_v7)

        row = db_at_v7.execute(
            "SELECT ingested_at FROM sources WHERE source_id = 's1'"
        ).fetchone()
        assert row[0] == "2024-06-15T12:00:00.000000+00:00"
        assert len(row[0]) == CANONICAL_LEN
        # Parses as aware UTC.
        assert _dt.datetime.fromisoformat(row[0]).tzinfo is not None

        claim = db_at_v7.execute(
            "SELECT created_at, updated_at FROM claims WHERE claim_id = 'c1'"
        ).fetchone()
        assert claim[0] == "2024-06-15T12:00:00.000000+00:00"
        assert claim[1] == "2024-06-15T12:00:00.000000+00:00"

        event = db_at_v7.execute(
            "SELECT created_at FROM events WHERE event_id = 'e1'"
        ).fetchone()
        assert event[0] == "2024-06-15T12:00:00.000000+00:00"

    def test_tz_without_micro_is_expanded(self, db_at_v7: sqlite3.Connection) -> None:
        _seed_source(db_at_v7, "s2", "2024-06-15T12:00:00+00:00")
        db_at_v7.commit()
        migrate_to_latest(db_at_v7)
        row = db_at_v7.execute(
            "SELECT ingested_at FROM sources WHERE source_id = 's2'"
        ).fetchone()
        assert row[0] == "2024-06-15T12:00:00.000000+00:00"

    def test_trailing_z_is_expanded(self, db_at_v7: sqlite3.Connection) -> None:
        _seed_source(db_at_v7, "s3", "2024-06-15T12:00:00Z")
        db_at_v7.commit()
        migrate_to_latest(db_at_v7)
        row = db_at_v7.execute(
            "SELECT ingested_at FROM sources WHERE source_id = 's3'"
        ).fetchone()
        assert row[0] == "2024-06-15T12:00:00.000000+00:00"

    def test_canonical_row_is_unchanged(self, db_at_v7: sqlite3.Connection) -> None:
        canonical = "2024-06-15T12:00:00.500000+00:00"
        _seed_source(db_at_v7, "s4", canonical)
        db_at_v7.commit()
        migrate_to_latest(db_at_v7)
        row = db_at_v7.execute(
            "SELECT ingested_at FROM sources WHERE source_id = 's4'"
        ).fetchone()
        assert row[0] == canonical

    def test_nullable_columns_stay_null(self, db_at_v7: sqlite3.Connection) -> None:
        """``claim_metadata.superseded_at`` is nullable; NULL must stay NULL."""
        _seed_source(db_at_v7, "s5", "2024-06-15T12:00:00")
        _seed_claim(db_at_v7, "c5", "s5", "2024-06-15T12:00:00", "2024-06-15T12:00:00")
        db_at_v7.execute(
            """INSERT INTO claim_metadata(claim_id, reaffirm_count, last_seen_at,
                                           superseded_by, superseded_at,
                                           created_at, updated_at)
               VALUES ('c5', 0, ?, NULL, NULL, ?, ?)""",
            ("2024-06-15T12:00:00", "2024-06-15T12:00:00", "2024-06-15T12:00:00"),
        )
        db_at_v7.commit()

        migrate_to_latest(db_at_v7)

        row = db_at_v7.execute(
            "SELECT last_seen_at, superseded_at, created_at, updated_at "
            "FROM claim_metadata WHERE claim_id = 'c5'"
        ).fetchone()
        assert row[0] == "2024-06-15T12:00:00.000000+00:00"
        assert row[1] is None
        assert row[2] == "2024-06-15T12:00:00.000000+00:00"
        assert row[3] == "2024-06-15T12:00:00.000000+00:00"


class TestIdempotency:
    def test_second_migrate_to_latest_is_noop(self, db_at_v7: sqlite3.Connection) -> None:
        _seed_source(db_at_v7, "si", "2024-06-15T12:00:00")
        db_at_v7.commit()
        first = migrate_to_latest(db_at_v7)
        second = migrate_to_latest(db_at_v7)
        assert 8 in first
        assert second == []  # Registry short-circuits.

    def test_direct_up_reapply_produces_identical_columns(
        self, db_at_v7: sqlite3.Connection
    ) -> None:
        _seed_source(db_at_v7, "sa", "2024-06-15T12:00:00")
        _seed_source(db_at_v7, "sb", "2024-06-15T12:00:00+00:00")
        _seed_source(db_at_v7, "sc", "2024-06-15T12:00:00.500000+00:00")
        db_at_v7.commit()
        migrate_to_latest(db_at_v7)
        after_first = sorted(_all_column(db_at_v7, "sources", "ingested_at"))

        # Run m0008.up() a second time by hand against the normalized corpus.
        m0008_normalize_naive_created_at.up(db_at_v7)
        db_at_v7.commit()
        after_second = sorted(_all_column(db_at_v7, "sources", "ingested_at"))

        assert after_first == after_second
        assert all(len(v) == CANONICAL_LEN for v in after_second)


class TestEventsAppendOnlyPreserved:
    def test_events_update_still_aborts(self, db_at_v7: sqlite3.Connection) -> None:
        _seed_event(db_at_v7, "e1", "2024-06-15T12:00:00")
        db_at_v7.commit()
        migrate_to_latest(db_at_v7)
        with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
            db_at_v7.execute(
                "UPDATE events SET actor = 'other' WHERE event_id = 'e1'"
            )

    def test_events_delete_still_aborts(self, db_at_v7: sqlite3.Connection) -> None:
        _seed_event(db_at_v7, "e2", "2024-06-15T12:00:00")
        db_at_v7.commit()
        migrate_to_latest(db_at_v7)
        with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
            db_at_v7.execute("DELETE FROM events WHERE event_id = 'e2'")


class TestEmptyCorpus:
    def test_migration_runs_cleanly_on_empty_db(self, db_at_v7: sqlite3.Connection) -> None:
        # No rows seeded — migration must still apply and bump schema_migrations.
        applied = migrate_to_latest(db_at_v7)
        assert 8 in applied
        versions = {
            r[0]
            for r in db_at_v7.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert 8 in versions


class TestCanonicalizer:
    """Direct exercise of the internal ``_canonicalize`` helper."""

    def test_naive_19_char(self) -> None:
        out = m0008_normalize_naive_created_at._canonicalize("2024-06-15T12:00:00")
        assert out == "2024-06-15T12:00:00.000000+00:00"

    def test_trailing_z(self) -> None:
        out = m0008_normalize_naive_created_at._canonicalize("2024-06-15T12:00:00Z")
        assert out == "2024-06-15T12:00:00.000000+00:00"

    def test_tz_without_micro(self) -> None:
        out = m0008_normalize_naive_created_at._canonicalize("2024-06-15T12:00:00+00:00")
        assert out == "2024-06-15T12:00:00.000000+00:00"

    def test_canonical_roundtrips(self) -> None:
        s = "2024-06-15T12:00:00.500000+00:00"
        assert m0008_normalize_naive_created_at._canonicalize(s) == s

    def test_non_utc_offset_is_converted_to_utc(self) -> None:
        # +08:00 at 20:00 is 12:00 UTC; canonical form normalizes to UTC.
        out = m0008_normalize_naive_created_at._canonicalize(
            "2024-06-15T20:00:00+08:00"
        )
        assert out == "2024-06-15T12:00:00.000000+00:00"


class TestDownIsNoop:
    def test_down_does_not_raise_and_does_not_revert(
        self, db_at_v7: sqlite3.Connection
    ) -> None:
        _seed_source(db_at_v7, "sd", "2024-06-15T12:00:00")
        db_at_v7.commit()
        migrate_to_latest(db_at_v7)
        before = _all_column(db_at_v7, "sources", "ingested_at")
        m0008_normalize_naive_created_at.down(db_at_v7)
        after = _all_column(db_at_v7, "sources", "ingested_at")
        assert before == after
