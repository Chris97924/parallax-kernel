"""P1-03: parallax_info() introspection tests."""

from __future__ import annotations

import pathlib

from bootstrap import bootstrap
from parallax import parallax_info
from parallax.ingest import ingest_claim, ingest_memory
from parallax.sqlite_store import connect


def test_parallax_info_empty_db(tmp_path: pathlib.Path) -> None:
    cfg = bootstrap(tmp_path / "inst")
    info = parallax_info(cfg.db_path)
    assert info.memories_count == 0
    assert info.claims_count == 0
    assert info.sources_count == 0
    assert info.events_count == 0
    # bootstrap now applies all migrations; latest version is the highest
    # entry in schema_migrations (== len(MIGRATIONS) at time of writing).
    from parallax.migrations import MIGRATIONS

    assert info.schema_version == max(m.version for m in MIGRATIONS)
    assert info.version


def test_parallax_info_populated_db(tmp_path: pathlib.Path) -> None:
    cfg = bootstrap(tmp_path / "inst")
    conn = connect(cfg.db_path)
    try:
        ingest_memory(
            conn, user_id="u1", title="t", summary="s",
            vault_path="v.md", source_id=None,
        )
        ingest_claim(
            conn, user_id="u1", subject="a", predicate="b",
            object_="c", source_id=None,
        )
    finally:
        conn.close()
    info = parallax_info(cfg.db_path)
    assert info.memories_count == 1
    assert info.claims_count == 1
    assert info.sources_count == 1
