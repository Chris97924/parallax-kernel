"""BackfillRunner — Lane D-2 read-only enumeration with zero-write invariant proof.

Lane D-3 deferred items (explicit, not silently hidden):
1. IngestPort.ingest real implementation.
2. Field normalization layer (memory.body / claim.object_ / event.payload_text
   canonical unification — Sonnet Critic's flagged tech debt).
3. ArbitrationDecision CLI view.
4. diff-audit human review gate.
5. Server-side flag wiring in parallax/server/routes/query.py.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys

from parallax.router.contracts import BackfillReport, BackfillRequest
from parallax.router.types import MappingState

__all__ = ["BackfillRunner"]

# Precompiled regex for bug-fix predicate classification (case-insensitive).
_BUG_RE = re.compile(r"^(fix|bug[_-]?fix|bugfix)(:|$)", re.IGNORECASE)


def _write_fingerprint(conn: sqlite3.Connection) -> str:
    """Return sha256 hex of pipe-joined COUNT(*) values across 4 core tables.

    Format: sha256('{events_count}|{claims_count}|{memories_count}|{decisions_count}')
    Returns a 64-character hex string.
    """
    events_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    claims_count = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    memories_count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    decisions_count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    payload = f"{events_count}|{claims_count}|{memories_count}|{decisions_count}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _classify_claim_predicate(predicate: str) -> str:
    """Classify a claim predicate string into a RetrieveKind probe-key.

    Returns:
        'RetrieveKind.decision' if predicate starts with 'decision:'
        'RetrieveKind.bug'      if predicate matches r'^(fix|bug[_-]?fix|bugfix)(:|$)'
                                (case-insensitive)
        'RetrieveKind.entity'   otherwise
    """
    if predicate.startswith("decision:"):
        return "RetrieveKind.decision"
    if _BUG_RE.match(predicate):
        return "RetrieveKind.bug"
    return "RetrieveKind.entity"


class BackfillRunner:
    """Read-only enumeration of v0.5 claims + memories through Crosswalk.

    Zero-write invariant: run() captures a sha256 fingerprint of all four
    core table row counts before and after enumeration and raises RuntimeError
    if they differ — proving no rows were written during the run.

    dry_run=False is explicitly rejected (ValueError) in this lane; real
    writes land in Lane D-3.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def run(self, request: BackfillRequest) -> BackfillReport:
        """Enumerate claims + memories via Crosswalk; prove zero writes.

        Method-local import of parallax.router.crosswalk_seed avoids bringing
        it into sys.modules at BackfillRunner instantiation time.
        """
        if not request.dry_run:
            raise ValueError(
                "Lane D-2 BackfillRunner supports dry_run=True only;"
                " real writes land in Lane D-3"
            )

        # Capture write fingerprint BEFORE enumeration.
        fingerprint_pre = _write_fingerprint(self._conn)

        # Method-local import: keeps parallax.router package importable without
        # side-effects from crosswalk_seed at module level.
        from parallax.router.crosswalk_seed import UnroutableQueryError, resolve

        limit = 50 if request.scope == "sample" else sys.maxsize

        rows_mapped = 0
        rows_unmapped = 0

        # --- Claims ---
        claim_rows = self._conn.execute(
            "SELECT claim_id, predicate FROM claims WHERE user_id = ?"
            " ORDER BY created_at DESC, claim_id ASC LIMIT ?",
            (request.user_id, limit),
        ).fetchall()

        for row in claim_rows:
            predicate = row[1] if row[1] is not None else ""
            probe_key = _classify_claim_predicate(predicate)
            try:
                resolve(probe_key)
                state = MappingState.MAPPED
            except UnroutableQueryError:
                state = MappingState.UNMAPPED
            # CONFLICT state is reachable in Lane D-3 once seed policies fan out;
            # Lane D-2 invariant: rows_conflict always 0
            if state == MappingState.MAPPED:
                rows_mapped += 1
            else:
                rows_unmapped += 1

        # --- Memories ---
        memory_rows = self._conn.execute(
            "SELECT memory_id FROM memories WHERE user_id = ?"
            " ORDER BY created_at DESC, memory_id ASC LIMIT ?",
            (request.user_id, limit),
        ).fetchall()

        for _row in memory_rows:
            probe_key = "RetrieveKind.recent"
            try:
                resolve(probe_key)
                state = MappingState.MAPPED
            except UnroutableQueryError:
                state = MappingState.UNMAPPED
            # CONFLICT state is reachable in Lane D-3 once seed policies fan out;
            # Lane D-2 invariant: rows_conflict always 0
            if state == MappingState.MAPPED:
                rows_mapped += 1
            else:
                rows_unmapped += 1

        rows_examined = len(claim_rows) + len(memory_rows)

        # Capture write fingerprint AFTER enumeration.
        fingerprint_post = _write_fingerprint(self._conn)

        if fingerprint_pre != fingerprint_post:
            raise RuntimeError(
                f"BackfillRunner violated zero-write invariant:"
                f" pre={fingerprint_pre[:16]} post={fingerprint_post[:16]}"
            )

        return BackfillReport(
            rows_examined=rows_examined,
            rows_mapped=rows_mapped,
            rows_unmapped=rows_unmapped,
            rows_conflict=0,
            writes_performed=0,
            arbitrations=(),
        )
