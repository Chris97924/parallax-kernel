"""BackfillRunner — Lane D-2 read-only enumeration with zero-write invariant proof."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid

from parallax.obs.log import get_logger as _get_logger
from parallax.router.contracts import BackfillReport, BackfillRequest
from parallax.router.types import MappingState
from parallax.sqlite_store import now_iso

__all__ = ["BackfillRunner"]

_log = _get_logger("parallax.router.backfill")

# Precompiled regex for bug-fix predicate classification (case-insensitive).
_BUG_RE = re.compile(r"^(fix|bug[_-]?fix|bugfix)(:|$)", re.IGNORECASE)

# H-1 hard cap: scope='all' still bounded to protect memory / latency.
_MAX_BACKFILL_ROWS = 10_000

# Identifier allowlist for _table_snapshot — sqlite has no bind-parameter for
# table names, so the f-string SELECT must be guarded against caller drift.
_SNAPSHOT_TABLES = frozenset({"events", "claims", "memories", "decisions", "crosswalk"})

# MED-1: chunk size for streaming hash to avoid loading large tables into memory.
_CHUNK_SIZE = 1000

# LOW-1: retry delays (seconds) for SQLITE_BUSY on BEGIN IMMEDIATE.
_BUSY_DELAYS = (0.1, 0.5, 2.0)


class _SqliteBusyError(RuntimeError):
    """Raised when SQLITE_BUSY persists after all retries."""


def _table_snapshot(conn: sqlite3.Connection, table: str) -> dict[str, str | int]:
    """Return count + content digest for *table*.

    MED-1: Uses chunked iteration (LIMIT/OFFSET) to avoid loading all rows
    into memory at once. Still hashes in canonical ORDER BY rowid order.
    """
    if table not in _SNAPSHOT_TABLES:
        raise ValueError(f"_table_snapshot: table {table!r} not in allowlist")
    digest = hashlib.sha256()
    total_count = 0
    offset = 0
    while True:
        chunk = conn.execute(
            f'SELECT * FROM "{table}" ORDER BY rowid LIMIT ? OFFSET ?',
            (_CHUNK_SIZE, offset),
        ).fetchall()
        if not chunk:
            break
        count = len(chunk)
        for row in chunk:
            for value in tuple(row):
                digest.update(b"\x1f")
                token = "<NULL>" if value is None else str(value)
                digest.update(token.encode("utf-8"))
                digest.update(b"\x1e")
            digest.update(b"\n")
        total_count += count
        offset += count
        if count < _CHUNK_SIZE:
            break
    return {"count": total_count, "digest": digest.hexdigest()}


def _core_fingerprint(conn: sqlite3.Connection) -> str:
    """Content-aware fingerprint of immutable core tables."""
    snapshot = {
        table: _table_snapshot(conn, table)
        for table in ("events", "claims", "memories", "decisions")
    }
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _crosswalk_exists(conn: sqlite3.Connection) -> bool:
    """Return True if the ``crosswalk`` table exists in *conn*."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='crosswalk'"
    ).fetchone()
    return row is not None


def _upsert_crosswalk(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    canonical_ref: str,
    target_kind: str,
    target_id: str,
    query_type: str | None,
    state: MappingState,
    content_hash: str,
    source_id: str | None,
    vault_path: str | None,
) -> None:
    """Insert or update a single crosswalk mapping row."""
    now = now_iso()
    conn.execute(
        """
        INSERT INTO crosswalk (
            user_id, canonical_ref, parallax_target_kind, parallax_target_id,
            query_type, state, content_hash, source_id, vault_path,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, canonical_ref) DO UPDATE SET
            parallax_target_kind = excluded.parallax_target_kind,
            parallax_target_id = excluded.parallax_target_id,
            query_type = excluded.query_type,
            state = excluded.state,
            content_hash = excluded.content_hash,
            source_id = excluded.source_id,
            vault_path = excluded.vault_path,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            canonical_ref,
            target_kind,
            target_id,
            query_type,
            state.value,
            content_hash,
            source_id,
            vault_path,
            now,
            now,
        ),
    )


def _classify_claim_predicate(predicate: str) -> str:
    """Classify a claim predicate string into a RetrieveKind probe-key.

    Returns:
        'RetrieveKind.decision' if predicate starts with 'decision:' (case-insensitive)
        'RetrieveKind.bug'      if predicate matches r'^(fix|bug[_-]?fix|bugfix)(:|$)'
                                (case-insensitive)
        'RetrieveKind.entity'   otherwise

    Both branches are case-insensitive to stay consistent with _BUG_RE
    (SF4 from Lane D-2 python review).
    """
    if predicate.lower().startswith("decision:"):
        return "RetrieveKind.decision"
    if _BUG_RE.match(predicate):
        return "RetrieveKind.bug"
    return "RetrieveKind.entity"


class BackfillRunner:
    """Read-only enumeration of v0.5 claims + memories through Crosswalk.

    Core-table invariant: run() fingerprints immutable core tables before/after
    and raises RuntimeError on mismatch.

    dry_run=False writes crosswalk mappings only.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialise with a SQLite connection for backfill operations."""
        self._conn = conn

    def _enumerate(
        self, request: BackfillRequest, *, write: bool = False
    ) -> tuple[int, int, int, int]:
        """Enumerate claims + memories; optionally write crosswalk rows.

        Returns (rows_mapped, rows_unmapped, writes_performed, rows_examined).
        """
        # Method-local import: keeps parallax.router package importable without
        # side-effects from crosswalk_seed at module level.
        from parallax.router.crosswalk_seed import UnroutableQueryError, resolve

        limit = 50 if request.scope == "sample" else _MAX_BACKFILL_ROWS

        rows_mapped = 0
        rows_unmapped = 0
        writes_performed = 0

        # --- Claims ---
        claim_rows = self._conn.execute(
            "SELECT claim_id, predicate, content_hash, source_id FROM claims WHERE user_id = ?"
            " ORDER BY created_at DESC, claim_id ASC LIMIT ?",
            (request.user_id, limit),
        ).fetchall()

        for row in claim_rows:
            predicate = row[1] if row[1] is not None else ""
            probe_key = _classify_claim_predicate(predicate)
            mapped_query_type = None
            try:
                mapped_query_type = resolve(probe_key)
                state = MappingState.MAPPED
            except UnroutableQueryError:
                state = MappingState.UNMAPPED

            if state == MappingState.MAPPED:
                rows_mapped += 1
            else:
                rows_unmapped += 1

            if write:
                _upsert_crosswalk(
                    self._conn,
                    user_id=request.user_id,
                    canonical_ref=f"claim:{row[0]}",
                    target_kind="claim",
                    target_id=row[0],
                    query_type=(mapped_query_type.value if mapped_query_type else None),
                    state=state,
                    content_hash=row[2],
                    source_id=row[3],
                    vault_path=None,
                )
                writes_performed += 1

        # --- Memories ---
        memory_rows = self._conn.execute(
            "SELECT memory_id, content_hash, source_id, vault_path "
            "FROM memories WHERE user_id = ?"
            " ORDER BY created_at DESC, memory_id ASC LIMIT ?",
            (request.user_id, limit),
        ).fetchall()

        for row in memory_rows:
            probe_key = "RetrieveKind.recent"
            mapped_query_type = None
            try:
                mapped_query_type = resolve(probe_key)
                state = MappingState.MAPPED
            except UnroutableQueryError:
                state = MappingState.UNMAPPED

            if state == MappingState.MAPPED:
                rows_mapped += 1
            else:
                rows_unmapped += 1

            if write:
                _upsert_crosswalk(
                    self._conn,
                    user_id=request.user_id,
                    canonical_ref=f"memory:{row[0]}",
                    target_kind="memory",
                    target_id=row[0],
                    query_type=(mapped_query_type.value if mapped_query_type else None),
                    state=state,
                    content_hash=row[1],
                    source_id=row[2],
                    vault_path=row[3],
                )
                writes_performed += 1

        rows_examined = len(claim_rows) + len(memory_rows)
        return rows_mapped, rows_unmapped, writes_performed, rows_examined

    def run(self, request: BackfillRequest) -> BackfillReport:
        """Enumerate claims + memories via Crosswalk.

        Invariants:
        - Core tables (events/claims/memories/decisions) are read-only in both
          dry_run and write mode.
        - dry_run=False writes only to crosswalk.
        - dry_run=False uses BEGIN IMMEDIATE with retry for SQLITE_BUSY (LOW-1).
        """
        if not request.dry_run and not _crosswalk_exists(self._conn):
            raise ValueError(
                "crosswalk table is required for dry_run=False; apply latest migrations first"
            )

        core_pre = _core_fingerprint(self._conn)
        crosswalk_pre = None
        if _crosswalk_exists(self._conn):
            crosswalk_pre = _table_snapshot(self._conn, "crosswalk")

        if request.dry_run:
            # Read-only path — no transaction needed.
            rows_mapped, rows_unmapped, writes_performed, rows_examined = self._enumerate(
                request, write=False
            )
        else:
            # LOW-1: BEGIN IMMEDIATE with retry for concurrent write safety.
            success = False
            result_tuple: tuple[int, int, int, int] | None = None
            for delay in _BUSY_DELAYS:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result_tuple = self._enumerate(request, write=True)
                        self._conn.commit()
                        success = True
                        break
                    except Exception:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                except sqlite3.OperationalError as exc:
                    if "database is locked" in str(exc):
                        time.sleep(delay)
                        continue
                    raise
            if not success:
                # Final attempt without a subsequent sleep
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result_tuple = self._enumerate(request, write=True)
                        self._conn.commit()
                        success = True
                    except Exception:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                except sqlite3.OperationalError as exc:
                    if "database is locked" in str(exc):
                        incident_id = str(uuid.uuid4())
                        raise _SqliteBusyError(
                            f"SQLITE_BUSY after retries; incident_id={incident_id}"
                        ) from exc
                    raise
            rows_mapped, rows_unmapped, writes_performed, rows_examined = result_tuple  # type: ignore[misc]

        core_post = _core_fingerprint(self._conn)
        crosswalk_post = None
        if _crosswalk_exists(self._conn):
            crosswalk_post = _table_snapshot(self._conn, "crosswalk")

        if core_pre != core_post:
            # M-2: do not leak fingerprint hex in the error message.
            incident_id = str(uuid.uuid4())
            _log.error(
                "core invariant violation",
                extra={
                    "event": "core_invariant_violation",
                    "incident_id": incident_id,
                    "pre": core_pre,
                    "post": core_post,
                },
            )
            raise RuntimeError(
                f"BackfillRunner violated read-only core invariant; incident_id={incident_id}"
            )

        if request.dry_run and crosswalk_pre != crosswalk_post:
            incident_id = str(uuid.uuid4())
            _log.error(
                "dry_run invariant violation",
                extra={
                    "event": "dry_run_invariant_violation",
                    "incident_id": incident_id,
                },
            )
            raise RuntimeError(
                f"BackfillRunner dry_run violated no-write invariant on crosswalk table;"
                f" incident_id={incident_id}"
            )

        return BackfillReport(
            rows_examined=rows_examined,
            rows_mapped=rows_mapped,
            rows_unmapped=rows_unmapped,
            rows_conflict=0,
            writes_performed=writes_performed,
            arbitrations=(),
        )

    def plan_upserts(
        self,
        user_id: str,
        scope: str = "sample",
    ) -> list[dict[str, str | None]]:
        """Return planned crosswalk upserts without writing, sorted by canonical_ref.

        Used by `parallax router backfill plan` to generate a human-readable diff
        of what `apply` would write.  Each entry has keys:
        canonical_ref, target_kind, target_id, state, query_type.

        No _crosswalk_exists() check needed: this method only reads core tables
        (claims, memories) and never touches the crosswalk table.
        """
        from parallax.router.crosswalk_seed import UnroutableQueryError, resolve

        limit = 50 if scope == "sample" else _MAX_BACKFILL_ROWS
        planned: list[dict[str, str | None]] = []

        claim_rows = self._conn.execute(
            "SELECT claim_id, predicate FROM claims WHERE user_id = ?"
            " ORDER BY created_at DESC, claim_id ASC LIMIT ?",
            (user_id, limit),
        ).fetchall()

        for row in claim_rows:
            predicate = row["predicate"] if row["predicate"] is not None else ""
            probe_key = _classify_claim_predicate(predicate)
            mapped_qt: object = None
            try:
                mapped_qt = resolve(probe_key)
                row_state = MappingState.MAPPED
            except UnroutableQueryError:
                row_state = MappingState.UNMAPPED
            planned.append({
                "canonical_ref": f"claim:{row['claim_id']}",
                "target_kind": "claim",
                "target_id": row["claim_id"],
                "state": row_state.value,
                "query_type": mapped_qt.value if mapped_qt is not None else None,
            })

        memory_rows = self._conn.execute(
            "SELECT memory_id FROM memories WHERE user_id = ?"
            " ORDER BY created_at DESC, memory_id ASC LIMIT ?",
            (user_id, limit),
        ).fetchall()

        for row in memory_rows:
            mapped_qt = None
            try:
                mapped_qt = resolve("RetrieveKind.recent")
                row_state = MappingState.MAPPED
            except UnroutableQueryError:
                row_state = MappingState.UNMAPPED
            planned.append({
                "canonical_ref": f"memory:{row['memory_id']}",
                "target_kind": "memory",
                "target_id": row["memory_id"],
                "state": row_state.value,
                "query_type": mapped_qt.value if mapped_qt is not None else None,
            })

        planned.sort(key=lambda r: r["canonical_ref"] or "")
        return planned
