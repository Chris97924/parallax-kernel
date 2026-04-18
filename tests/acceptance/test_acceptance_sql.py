"""SQL acceptance harness for Parallax v0.2.0 (Phase 2 closeout).

The four sibling .sql files are the SSoT for the four Phase-2 acceptance
questions (canonical-exists / identity / state-traceable / rebuild-identical).
This module reads them, parametrizes the ? placeholders, and asserts on the
result rows. No SQL string is duplicated inline.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from parallax.events import record_claim_state_changed
from parallax.index import rebuild_index
from parallax.ingest import ingest_claim, ingest_memory, synthetic_direct_source_id

ACCEPTANCE_DIR = pathlib.Path(__file__).parent
INDEX_NAME = "chroma"


def _read_sql(filename: str) -> str:
    return (ACCEPTANCE_DIR / filename).read_text(encoding="utf-8")


def _split_statements(sql: str) -> list[str]:
    """Strip `--` comment lines, then split the remainder on `;`.

    Comments are stripped FIRST because a comment line may legitimately
    contain a `;` (English prose) and we don't want it to fracture the
    statement boundaries.
    """
    code_lines = [
        line for line in sql.splitlines() if line.strip() and not line.strip().startswith("--")
    ]
    code = "\n".join(code_lines)
    return [stmt.strip() for stmt in code.split(";") if stmt.strip()]


@pytest.fixture()
def seeded(conn: sqlite3.Connection) -> Iterator[dict[str, str]]:
    """Seed one of every canonical object so the SQL harness has data to read."""
    user_id = "acceptance-user"
    source_id = synthetic_direct_source_id(user_id)
    memory_id = ingest_memory(
        conn,
        user_id=user_id,
        title="Acceptance harness memory",
        summary="Seed row for the v0.2.0 SQL acceptance suite.",
        vault_path="users/acceptance-user/memories/seed.md",
    )
    claim_id = ingest_claim(
        conn,
        user_id=user_id,
        subject="parallax",
        predicate="ships",
        object_="v0.2.0",
        source_id=source_id,
    )
    event_id = record_claim_state_changed(
        conn,
        user_id=user_id,
        claim_id=claim_id,
        from_state="pending",
        to_state="confirmed",
    )
    rebuild_index(conn, INDEX_NAME)

    yield {
        "source_id": source_id,
        "memory_id": memory_id,
        "claim_id": claim_id,
        "event_id": event_id,
    }


def test_01_canonical_exists(conn: sqlite3.Connection, seeded: dict[str, str]) -> None:
    statements = _split_statements(_read_sql("01_canonical.sql"))
    assert len(statements) == 2, "01_canonical.sql must contain exactly two SELECTs"
    counts = [conn.execute(stmt).fetchone()[0] for stmt in statements]
    assert all(c >= 1 for c in counts), f"DB canonical empty: {counts}"


@pytest.mark.parametrize(
    "stmt_index,seed_key",
    [
        (0, "claim_id"),
        (1, "memory_id"),
        (2, "source_id"),
        (3, "event_id"),
    ],
)
def test_02_identity_pks(
    conn: sqlite3.Connection,
    seeded: dict[str, str],
    stmt_index: int,
    seed_key: str,
) -> None:
    statements = _split_statements(_read_sql("02_identity.sql"))
    assert len(statements) == 5, "02_identity.sql must contain exactly five SELECTs"
    count = conn.execute(statements[stmt_index], (seeded[seed_key],)).fetchone()[0]
    assert count == 1, f"PK lookup for {seed_key}={seeded[seed_key]!r} returned {count}, want 1"


def test_02_identity_claim_source_join(
    conn: sqlite3.Connection, seeded: dict[str, str]
) -> None:
    statements = _split_statements(_read_sql("02_identity.sql"))
    join_count = conn.execute(statements[4], (seeded["claim_id"],)).fetchone()[0]
    assert join_count == 1, (
        f"claim->source JOIN dropped row: claim_id={seeded['claim_id']!r} got {join_count}"
    )


def test_03_state_replayable(
    conn: sqlite3.Connection, seeded: dict[str, str]
) -> None:
    statements = _split_statements(_read_sql("03_state_traceable.sql"))
    assert len(statements) == 1, "03_state_traceable.sql must contain exactly one SELECT"
    rows = conn.execute(statements[0], ("claim", seeded["claim_id"])).fetchall()
    assert len(rows) >= 1, "no events found for the seeded claim"
    for actor, created_at, payload_json in rows:
        assert actor, "event actor must be non-empty"
        assert created_at, "event created_at must be non-empty"
        json.loads(payload_json)


def test_04_rebuild_idempotent(
    conn: sqlite3.Connection, seeded: dict[str, str]
) -> None:
    statements = _split_statements(_read_sql("04_rebuild_identical.sql"))
    assert len(statements) == 1, "04_rebuild_identical.sql must contain exactly one SELECT"
    sql = statements[0]

    pre_rows = conn.execute(sql, (INDEX_NAME,)).fetchall()
    assert pre_rows, "seed fixture should have produced at least one index_state row"
    pre_max_version = max(row[1] for row in pre_rows)
    pre_doc_count = pre_rows[-1][2]
    pre_state = pre_rows[-1][3]

    rebuild_index(conn, INDEX_NAME)

    post_rows = conn.execute(sql, (INDEX_NAME,)).fetchall()
    post_max_version = max(row[1] for row in post_rows)
    post_doc_count = post_rows[-1][2]
    post_state = post_rows[-1][3]

    assert post_max_version > pre_max_version, "version must increment on rebuild"
    assert post_doc_count == pre_doc_count, (
        f"doc_count drifted across idempotent rebuild: {pre_doc_count} -> {post_doc_count}"
    )
    assert post_state == pre_state, (
        f"state drifted across idempotent rebuild: {pre_state} -> {post_state}"
    )
