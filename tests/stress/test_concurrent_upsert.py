"""Concurrency stress: 10 threads x 100 iter on identical content_hash.

Verifies the v0.1.1 phantom-ID fix (INSERT OR IGNORE + re-SELECT on the
UNIQUE index) holds under high concurrent duplicate pressure:

* Memory variant: 1000 concurrent ingests of the SAME content ->
  exactly 1 persisted row, all 1000 returned ids equal to that row's id.
* Claim variant: same contract on the claims table.
* Mixed-content variant: 1000 ingests rotating through 50 distinct
  logical contents -> exactly 50 rows, zero phantom ids, every returned
  id resolves to a real persisted row.

Each thread opens its own ``sqlite3.Connection`` (check_same_thread=False
on shared connections is a SQLite foot-gun; a per-thread connection pool is
the robust answer).
"""

from __future__ import annotations

import pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from parallax.ingest import ingest_claim, ingest_memory
from parallax.sqlite_store import connect, query

_THREADS = 10
_ITERS = 100
_USER = "stress"


def _per_thread_memory(
    db_path: pathlib.Path, title: str, summary: str, path: str, iters: int
) -> list[str]:
    conn = connect(db_path)
    try:
        ids: list[str] = []
        for _ in range(iters):
            ids.append(
                ingest_memory(
                    conn,
                    user_id=_USER,
                    title=title,
                    summary=summary,
                    vault_path=path,
                )
            )
        return ids
    finally:
        conn.close()


def _per_thread_claim(
    db_path: pathlib.Path, subject: str, predicate: str, obj: str, iters: int
) -> list[str]:
    conn = connect(db_path)
    try:
        ids: list[str] = []
        for _ in range(iters):
            ids.append(
                ingest_claim(
                    conn,
                    user_id=_USER,
                    subject=subject,
                    predicate=predicate,
                    object_=obj,
                )
            )
        return ids
    finally:
        conn.close()


def _per_thread_rotating_memory(
    db_path: pathlib.Path, titles: list[str], iters: int, thread_idx: int
) -> list[str]:
    conn = connect(db_path)
    try:
        ids: list[str] = []
        for i in range(iters):
            title = titles[(thread_idx + i) % len(titles)]
            ids.append(
                ingest_memory(
                    conn,
                    user_id=_USER,
                    title=title,
                    summary="s",
                    vault_path=f"{title}.md",
                )
            )
        return ids
    finally:
        conn.close()


class TestConcurrentIdenticalMemory:
    def test_identical_memory_collapses_to_one_row(self, db_path: pathlib.Path) -> None:
        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [
                pool.submit(
                    _per_thread_memory, db_path, "T", "S", "v.md", _ITERS
                )
                for _ in range(_THREADS)
            ]
            all_ids: list[str] = []
            for fut in as_completed(futures):
                all_ids.extend(fut.result())

        assert len(all_ids) == _THREADS * _ITERS
        persisted = set(all_ids)
        assert len(persisted) == 1, (
            f"phantom IDs detected: {len(persisted)} distinct ids "
            f"from {_THREADS * _ITERS} concurrent duplicate writes"
        )

        conn = connect(db_path)
        try:
            n = query(conn, "SELECT COUNT(*) AS n FROM memories", ())[0]["n"]
            assert n == 1
            persisted_id = next(iter(persisted))
            row = query(
                conn,
                "SELECT memory_id FROM memories WHERE memory_id = ?",
                (persisted_id,),
            )
            assert len(row) == 1
        finally:
            conn.close()


class TestConcurrentIdenticalClaim:
    def test_identical_claim_collapses_to_one_row(self, db_path: pathlib.Path) -> None:
        # Pre-create the synthetic source in the main thread so worker
        # connections don't race the lazy source insert. ingest_memory /
        # ingest_claim both use INSERT OR IGNORE on sources so races are
        # still tolerated, but making the setup explicit makes the test's
        # intent clearer.
        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [
                pool.submit(
                    _per_thread_claim, db_path, "chris", "likes", "coffee", _ITERS
                )
                for _ in range(_THREADS)
            ]
            all_ids: list[str] = []
            for fut in as_completed(futures):
                all_ids.extend(fut.result())

        assert len(all_ids) == _THREADS * _ITERS
        persisted = set(all_ids)
        assert len(persisted) == 1, (
            f"phantom IDs detected: {len(persisted)} distinct ids"
        )

        conn = connect(db_path)
        try:
            n = query(conn, "SELECT COUNT(*) AS n FROM claims", ())[0]["n"]
            assert n == 1
        finally:
            conn.close()


class TestConcurrentMixedContent:
    def test_rotating_contents_no_lost_upsert(self, db_path: pathlib.Path) -> None:
        titles = [f"title-{i}" for i in range(50)]

        with ThreadPoolExecutor(max_workers=_THREADS) as pool:
            futures = [
                pool.submit(
                    _per_thread_rotating_memory, db_path, titles, _ITERS, tidx
                )
                for tidx in range(_THREADS)
            ]
            all_ids: list[str] = []
            for fut in as_completed(futures):
                all_ids.extend(fut.result())

        assert len(all_ids) == _THREADS * _ITERS

        conn = connect(db_path)
        try:
            n = query(conn, "SELECT COUNT(*) AS n FROM memories", ())[0]["n"]
            assert n == 50, f"expected 50 distinct rows; got {n}"

            # Every returned id must resolve to a real persisted row.
            persisted_ids = {
                row["memory_id"]
                for row in query(conn, "SELECT memory_id FROM memories", ())
            }
            returned_ids = set(all_ids)
            phantoms = returned_ids - persisted_ids
            assert not phantoms, f"phantom ids not persisted: {phantoms}"

            # And every persisted row must have been touched by at least
            # one caller (no lost UPSERT).
            lost = persisted_ids - returned_ids
            assert not lost, f"persisted rows never returned to a caller: {lost}"
        finally:
            conn.close()
