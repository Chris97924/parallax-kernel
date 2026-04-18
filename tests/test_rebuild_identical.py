"""P0-02: rebuild-identical 4-sentence test.

Bootstraps a fresh DB, ingests a fixed 3-memory + 3-claim fixture, records
the content_hash set, deletes the DB, bootstraps again, re-ingests the same
fixture, and asserts the hash sets match byte-for-byte. This is the
canonical "rebuild identical" guarantee (Phase 0 contract).
"""

from __future__ import annotations

import pathlib
import shutil

from bootstrap import bootstrap
from parallax.ingest import ingest_claim, ingest_memory
from parallax.sqlite_store import connect, query


FIXTURE_MEMORIES = [
    dict(user_id="u1", title="m1", summary="s1", vault_path="users/u1/m1.md"),
    dict(user_id="u1", title="m2", summary="s2", vault_path="users/u1/m2.md"),
    dict(user_id="u2", title="m3", summary="s3", vault_path="users/u2/m3.md"),
]

FIXTURE_CLAIMS = [
    dict(user_id="u1", subject="chris", predicate="likes", object_="tea"),
    dict(user_id="u1", subject="chris", predicate="lives_in", object_="taipei"),
    dict(user_id="u2", subject="alex", predicate="uses", object_="python"),
]


def _ingest_fixture(db_path: pathlib.Path) -> tuple[set[str], set[str]]:
    conn = connect(db_path)
    try:
        for m in FIXTURE_MEMORIES:
            ingest_memory(conn, source_id=None, **m)
        for c in FIXTURE_CLAIMS:
            ingest_claim(conn, source_id=None, **c)
        mem_hashes = {r["content_hash"] for r in query(conn, "SELECT content_hash FROM memories")}
        cla_hashes = {r["content_hash"] for r in query(conn, "SELECT content_hash FROM claims")}
    finally:
        conn.close()
    return mem_hashes, cla_hashes


def test_rebuild_identical_content_hashes(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "instance"
    cfg = bootstrap(target)
    first_mem, first_claims = _ingest_fixture(cfg.db_path)

    shutil.rmtree(target)

    cfg2 = bootstrap(target)
    second_mem, second_claims = _ingest_fixture(cfg2.db_path)

    assert first_mem == second_mem
    assert first_claims == second_claims
    assert len(first_mem) == 3
    assert len(first_claims) == 3
