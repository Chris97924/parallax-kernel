"""BackfillRunner — Lane D-2 read-only enumeration with zero-write invariant proof."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import nullcontext

from parallax.router.contracts import BackfillReport, BackfillRequest
from parallax.router.types import MappingState
from parallax.sqlite_store import now_iso

__all__ = ["BackfillRunner"]

# Precompiled regex for bug-fix predicate classification (case-insensitive).
_BUG_RE = re.compile(r"^(fix|bug[_-]?fix|bugfix)(:|$)", re.IGNORECASE)

# H-1 hard cap: scope='all' still bounded to protect memory / latency.
_MAX_BACKFILL_ROWS = 10_000

# Identifier allowlist for _table_snapshot — sqlite has no bind-parameter for
# table names, so the f-string SELECT must be guarded against caller drift.
_SNAPSHOT_TABLES = frozenset({"events", "claims", "memories", "decisions", "crosswalk"})


def _table_snapshot(conn: sqlite3.Connection, table: str) -> dict[str, str | int]:
    """Return count + content digest for *table*.

    Uses SELECT * ORDER BY rowid to stay schema-agnostic and still catch
    in-place UPDATE changes that count-only fingerprints miss.
    """
    if table not in _SNAPSHOT_TABLES:
        raise ValueError(f"_table_snapshot: table {table!r} not in allowlist")
    rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    digest = hashlib.sha256()
    for row in rows:
        for value in tuple(row):
            digest.update(b"\x1f")
            token = "<NULL>" if value is None else str(value)
            digest.update(token.encode("utf-8"))
            digest.update(b"\x1e")
        digest.update(b"\n")
    return {"count": len(rows), "digest": digest.hexdigest()}


def _core_fingerprint(conn: sqlite3.Connection) -> str:
    """Content-aware fingerprint of immutable core tables."""
    snapshot = {
        table: _table_snapshot(conn, table)
        for table in ("events", "claims", "memories", "decisions")
    }
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _crosswalk_exists(conn: sqlite3.Connection) -> bool:
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
        self._conn = conn

    def run(self, request: BackfillRequest) -> BackfillReport:
        """Enumerate claims + memories via Crosswalk.

        Invariants:
        - Core tables (events/claims/memories/decisions) are read-only in both
          dry_run and write mode.
        - dry_run=False writes only to crosswalk.
        """
        if not request.dry_run and not _crosswalk_exists(self._conn):
            raise ValueError(
                "crosswalk table is required for dry_run=False; apply latest migrations first"
            )

        core_pre = _core_fingerprint(self._conn)
        crosswalk_pre = None
        if _crosswalk_exists(self._conn):
            crosswalk_pre = _table_snapshot(self._conn, "crosswalk")

        # Method-local import: keeps parallax.router package importable without
        # side-effects from crosswalk_seed at module level.
        from parallax.router.crosswalk_seed import UnroutableQueryError, resolve

        limit = 50 if request.scope == "sample" else _MAX_BACKFILL_ROWS

        rows_mapped = 0
        rows_unmapped = 0
        writes_performed = 0

        write_context = self._conn if not request.dry_run else nullcontext()
        with write_context:
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

                if not request.dry_run:
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

                if not request.dry_run:
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

            core_post = _core_fingerprint(self._conn)
            crosswalk_post = None
            if _crosswalk_exists(self._conn):
                crosswalk_post = _table_snapshot(self._conn, "crosswalk")

            if core_pre != core_post:
                raise RuntimeError(
                    "BackfillRunner violated read-only core invariant:"
                    f" pre={core_pre[:16]} post={core_post[:16]}"
                )

            if request.dry_run and crosswalk_pre != crosswalk_post:
                raise RuntimeError(
                    "BackfillRunner dry_run violated no-write invariant on crosswalk table"
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
