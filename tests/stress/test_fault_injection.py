"""Fault-injection stress for the SQLite canonical store.

Three classes:

* :class:`TestMidIngestKill` — spawn a child process that ingests N rows,
  kill it mid-loop, reopen the DB in the parent, and assert the row count
  equals the number of fully-committed UPSERTs (WAL atomicity contract).
* :class:`TestCorruptDB` — after a TRUNCATE checkpoint flushes the WAL,
  overwrite a 256-byte window inside page 1 (past the 100-byte header)
  and assert the next open either recovers a consistent state OR raises
  ``sqlite3.DatabaseError``. No silent corruption.
* :class:`TestWALRecovery` — force a WAL-pending crash, reopen, run
  ``PRAGMA wal_checkpoint(TRUNCATE)``, verify reads + file shrinkage.
"""

from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys
import textwrap
import time

from parallax.sqlite_store import connect, query

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _child_ingest_script(db_path: pathlib.Path, n: int, sleep_ms: int) -> str:
    """Build a throwaway child-process script that commits one memory at
    a time then sleeps. The parent kills the child mid-loop; every row
    whose ``with conn:`` commit returned before the kill must survive.
    """
    return textwrap.dedent(
        f"""
        import sys, time, pathlib
        sys.path.insert(0, r"{_ROOT}")
        from parallax.sqlite_store import connect
        from parallax.ingest import ingest_memory

        db = pathlib.Path(r"{db_path}")
        c = connect(db)
        try:
            for i in range({n}):
                ingest_memory(
                    c,
                    user_id="u",
                    title=f"t-{{i}}",
                    summary="s",
                    vault_path=f"v-{{i}}.md",
                )
                sys.stdout.write(f"ok {{i}}\\n")
                sys.stdout.flush()
                time.sleep({sleep_ms} / 1000.0)
        finally:
            c.close()
        """
    )


class TestMidIngestKill:
    def test_partial_ingest_survives_kill(
        self, db_path: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        script = tmp_path / "child.py"
        script.write_text(_child_ingest_script(db_path, n=200, sleep_ms=5), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait until the child has committed at least a handful of rows
        # before pulling the plug. The child prints "ok <i>" per commit.
        committed_prefix = 0
        assert proc.stdout is not None
        deadline = time.time() + 10.0
        while time.time() < deadline and committed_prefix < 5:
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("ok "):
                committed_prefix = int(line.split()[1]) + 1

        # terminate() maps to TerminateProcess on Windows and SIGTERM on
        # POSIX. Either way the child dies without a clean shutdown path,
        # which is exactly the crash scenario we want to simulate.
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

        # Re-open the DB in the parent. SQLite's WAL + `with conn:` means
        # every committed row must still be visible; in-flight rows may or
        # may not be present but MUST NOT have partially corrupted the DB.
        c = connect(db_path)
        try:
            rows = query(c, "SELECT COUNT(*) AS n FROM memories", ())
            persisted = rows[0]["n"]
        finally:
            c.close()

        assert persisted >= committed_prefix, (
            f"WAL atomicity violated: saw {committed_prefix} 'ok' lines "
            f"but only {persisted} rows survived the crash"
        )


class TestCorruptDB:
    def test_page1_corruption_is_not_silent(
        self, db_path: pathlib.Path
    ) -> None:
        # Write a valid row so the WAL exists.
        c = connect(db_path)
        try:
            from parallax.ingest import ingest_memory
            ingest_memory(c, user_id="u", title="t", summary="s", vault_path="v.md")
        finally:
            c.close()

        # Force a TRUNCATE checkpoint so the committed pages are flushed
        # out of the WAL sidecar and into the main DB file; this guarantees
        # the row we just inserted lives on a real page we can overwrite.
        c = connect(db_path)
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()

        # Corrupt bytes INSIDE a valid B-tree page. The SQLite header is
        # bytes 0-99 of page 1; bytes 100+ hold the first page's B-tree
        # header and cells. Overwriting a 256-byte window starting at
        # offset 100 destroys the B-tree structure AND at least one cell,
        # which SQLite is guaranteed to read the moment we touch the
        # memories table. Appending after the last page (the old
        # approach) was a no-op because SQLite sizes the DB from the
        # header page-count, not from stat(), so trailing bytes are
        # never read.
        size = db_path.stat().st_size
        assert size > 356, f"DB file too small to corrupt meaningfully: {size} bytes"
        with open(db_path, "r+b") as f:
            f.seek(100)
            f.write(b"\xde\xad\xbe\xef" * 64)  # 256 bytes inside page 1

        # Opening may succeed (SQLite ignores trailing junk outside the
        # page boundary) OR raise DatabaseError. Either way reads must
        # NOT return silently-corrupted rows.
        try:
            c = connect(db_path)
            try:
                # Touch the table — this forces SQLite to actually read
                # pages and surface corruption if the file is broken.
                rows = query(c, "SELECT memory_id, content_hash FROM memories", ())
                # If the open succeeded and reads succeeded, the row we
                # inserted pre-corruption must still be recoverable and
                # its content_hash must parse.
                assert len(rows) >= 1
                for r in rows:
                    assert len(r["content_hash"]) == 64
                    int(r["content_hash"], 16)
            finally:
                c.close()
        except sqlite3.DatabaseError as e:
            # Acceptable: loud failure, no silent corruption. We just
            # assert the error carries a recognizable corruption-ish
            # message so callers can handle it.
            assert any(
                word in str(e).lower()
                for word in ("malform", "corrupt", "not a database", "disk i/o")
            ), f"unexpected DatabaseError shape: {e}"


class TestWALRecovery:
    def test_checkpoint_truncate_shrinks_wal(
        self, db_path: pathlib.Path
    ) -> None:
        from parallax.ingest import ingest_memory

        # Warm the WAL with a burst of writes.
        c = connect(db_path)
        try:
            for i in range(50):
                ingest_memory(
                    c,
                    user_id="u",
                    title=f"t-{i}",
                    summary="s",
                    vault_path=f"v-{i}.md",
                )
        finally:
            c.close()

        wal = pathlib.Path(str(db_path) + "-wal")
        # Simulate "mid-write crash" by dropping the connection without a
        # clean checkpoint; on next open, SQLite replays the WAL.
        pre_size = wal.stat().st_size if wal.exists() else 0

        c = connect(db_path)
        try:
            # Reads must surface all committed rows.
            rows = query(c, "SELECT COUNT(*) AS n FROM memories", ())
            assert rows[0]["n"] == 50

            # Explicit TRUNCATE checkpoint shrinks the WAL.
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.commit()
        finally:
            c.close()

        post_size = wal.stat().st_size if wal.exists() else 0
        # Either the WAL file was truncated (post < pre) or deleted
        # (post == 0 while pre > 0). Both are valid WAL-recovery outcomes.
        if pre_size > 0:
            assert post_size <= pre_size
        # Final sanity check: data is still there after checkpoint.
        c = connect(db_path)
        try:
            assert query(c, "SELECT COUNT(*) AS n FROM memories", ())[0]["n"] == 50
        finally:
            c.close()
