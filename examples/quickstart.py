"""Parallax Kernel quickstart: bootstrap → ingest → retrieve."""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bootstrap import bootstrap  # noqa: E402
from parallax import ingest_memory, memories_by_user  # noqa: E402
from parallax.sqlite_store import connect  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = bootstrap(pathlib.Path(tmp))
        conn = connect(cfg.db_path)
        try:
            mid = ingest_memory(
                conn,
                user_id="chris",
                title="hello parallax",
                summary="first memory",
                vault_path="hello.md",
            )
            print(f"ingested memory_id={mid}")
            for row in memories_by_user(conn, "chris"):
                print(f"  {row['memory_id']} :: {row['title']}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
