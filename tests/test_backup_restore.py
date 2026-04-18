"""Round-trip tests for ``parallax backup`` / ``parallax restore`` (v0.2.1).

The round-trip test is the acceptance contract: seed a realistic db,
back it up, wipe the db + vault, restore, then re-run the four
acceptance SQL files and assert identical results. If any of those
SQL snapshots drift, the backup/restore is broken by definition.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sqlite3
import tarfile
from collections.abc import Callable
from typing import Any

import pytest

from parallax.backup import BackupManifest, create_backup
from parallax.cli import main as cli_main
from parallax.events import record_claim_state_changed
from parallax.index import rebuild_index
from parallax.ingest import ingest_claim, ingest_memory, synthetic_direct_source_id
from parallax.migrations import migrate_to_latest
from parallax.restore import RestoreVerificationError, restore_backup
from parallax.sqlite_store import connect
from tests.acceptance.test_acceptance_sql import _split_statements

ACCEPTANCE_DIR = (
    pathlib.Path(__file__).parent / "acceptance"
)
USER_ID = "backup-user"


@dataclasses.dataclass(frozen=True)
class _Cfg:
    db_path: pathlib.Path
    vault_path: pathlib.Path
    schema_path: pathlib.Path


def _make_cfg(tmp_path: pathlib.Path) -> _Cfg:
    return _Cfg(
        db_path=tmp_path / "db" / "parallax.db",
        vault_path=tmp_path / "vault",
        schema_path=tmp_path / "schema.sql",
    )


def _seed(cfg: _Cfg) -> dict[str, str]:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.vault_path.mkdir(parents=True, exist_ok=True)
    mem_dir = cfg.vault_path / "users" / USER_ID / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "seed.md").write_text("# seed\n\nbackup round-trip fixture.\n", "utf-8")

    conn = connect(cfg.db_path)
    try:
        migrate_to_latest(conn)
        source_id = synthetic_direct_source_id(USER_ID)
        memory_id = ingest_memory(
            conn,
            user_id=USER_ID,
            title="Backup round-trip memory",
            summary="Seed row for v0.2.1 backup/restore.",
            vault_path=f"users/{USER_ID}/memories/seed.md",
        )
        claim_id = ingest_claim(
            conn,
            user_id=USER_ID,
            subject="parallax",
            predicate="ships",
            object_="v0.2.1",
            source_id=source_id,
        )
        event_id = record_claim_state_changed(
            conn,
            user_id=USER_ID,
            claim_id=claim_id,
            from_state="pending",
            to_state="confirmed",
        )
        rebuild_index(conn, "chroma")
    finally:
        conn.close()

    return {
        "source_id": source_id,
        "memory_id": memory_id,
        "claim_id": claim_id,
        "event_id": event_id,
    }


def _snapshot_acceptance_sql(db_path: pathlib.Path, seeded: dict[str, str]) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    try:
        snap: dict[str, Any] = {}
        s01 = _split_statements((ACCEPTANCE_DIR / "01_canonical.sql").read_text("utf-8"))
        snap["01"] = [conn.execute(stmt).fetchone()[0] for stmt in s01]

        s02 = _split_statements((ACCEPTANCE_DIR / "02_identity.sql").read_text("utf-8"))
        snap["02_pks"] = [
            conn.execute(s02[0], (seeded["claim_id"],)).fetchone()[0],
            conn.execute(s02[1], (seeded["memory_id"],)).fetchone()[0],
            conn.execute(s02[2], (seeded["source_id"],)).fetchone()[0],
            conn.execute(s02[3], (seeded["event_id"],)).fetchone()[0],
        ]
        snap["02_join"] = conn.execute(s02[4], (seeded["claim_id"],)).fetchone()[0]

        s03 = _split_statements(
            (ACCEPTANCE_DIR / "03_state_traceable.sql").read_text("utf-8")
        )
        rows03 = conn.execute(s03[0], ("claim", seeded["claim_id"])).fetchall()
        snap["03"] = [(r[0], r[2]) for r in rows03]  # actor + payload_json (drop wall-clock)

        s04 = _split_statements(
            (ACCEPTANCE_DIR / "04_rebuild_identical.sql").read_text("utf-8")
        )
        rows04 = conn.execute(s04[0], ("chroma",)).fetchall()
        snap["04"] = [(r[0], r[1], r[2], r[3]) for r in rows04]
    finally:
        conn.close()
    return snap


# -------- acceptance round-trip -----------------------------------------------


def test_backup_restore_round_trip_preserves_acceptance_sql(tmp_path: pathlib.Path) -> None:
    cfg = _make_cfg(tmp_path)
    seeded = _seed(cfg)
    before = _snapshot_acceptance_sql(cfg.db_path, seeded)

    archive = tmp_path / "backup.tar.gz"
    manifest = create_backup(cfg, archive)
    assert archive.is_file()
    assert manifest.row_counts["memories"] >= 1
    assert manifest.row_counts["claims"] >= 1

    # Wipe the canonical store.
    cfg.db_path.unlink()
    import shutil
    shutil.rmtree(cfg.vault_path)
    assert not cfg.db_path.exists()
    assert not cfg.vault_path.exists()

    restored = restore_backup(cfg, archive)
    assert restored.db_sha256 == manifest.db_sha256
    assert cfg.db_path.is_file()
    assert cfg.vault_path.is_dir()

    after = _snapshot_acceptance_sql(cfg.db_path, seeded)
    assert after == before, f"acceptance SQL drift after round-trip: {before} vs {after}"


# -------- unit-level error cases ---------------------------------------------


def test_backup_raises_if_db_missing(tmp_path: pathlib.Path) -> None:
    cfg = _make_cfg(tmp_path)
    with pytest.raises(FileNotFoundError):
        create_backup(cfg, tmp_path / "backup.tar.gz")


def test_backup_raises_if_archive_exists(tmp_path: pathlib.Path) -> None:
    cfg = _make_cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"preexisting")
    with pytest.raises(FileExistsError):
        create_backup(cfg, archive)


def _repack_with_tampered_db(
    src: pathlib.Path, dst: pathlib.Path, mutate_db: Callable[[bytes], bytes]
) -> None:
    """Rebuild ``src`` into ``dst`` replacing db/parallax.db with ``mutate_db(...)``."""
    import io
    with tarfile.open(src, "r:gz") as src_tar, tarfile.open(dst, "w:gz") as dst_tar:
        for member in src_tar.getmembers():
            data_file = src_tar.extractfile(member) if member.isfile() else None
            if member.name.replace("\\", "/") == "db/parallax.db" and data_file is not None:
                new_bytes = mutate_db(data_file.read())
                new_info = tarfile.TarInfo(name=member.name)
                new_info.size = len(new_bytes)
                new_info.mtime = member.mtime
                dst_tar.addfile(new_info, io.BytesIO(new_bytes))
            elif member.isfile() and data_file is not None:
                dst_tar.addfile(member, data_file)
            else:
                dst_tar.addfile(member)


def test_restore_verify_detects_tampering(tmp_path: pathlib.Path) -> None:
    cfg = _make_cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)
    tampered = tmp_path / "tampered.tar.gz"
    # Shrink the db to a 1-byte placeholder — sha256 + every row count breaks.
    _repack_with_tampered_db(archive, tampered, mutate_db=lambda _b: b"\0")

    cfg.db_path.unlink()
    with pytest.raises(RestoreVerificationError) as exc_info:
        restore_backup(cfg, tampered)
    msg = str(exc_info.value)
    assert "db_sha256" in msg or "row_counts" in msg


def test_restore_preserves_existing_db_as_backup(tmp_path: pathlib.Path) -> None:
    cfg = _make_cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)

    # Leave the old db in place so restore has something to move aside.
    restore_backup(cfg, archive)

    siblings = list(cfg.db_path.parent.glob(cfg.db_path.name + ".bak-*"))
    assert len(siblings) == 1, f"expected 1 .bak sibling, got {siblings}"


# -------- CLI integration ----------------------------------------------------


def test_cli_backup_and_restore_round_trip(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    seeded = _seed(cfg)
    before = _snapshot_acceptance_sql(cfg.db_path, seeded)

    monkeypatch.setenv("PARALLAX_DB_PATH", str(cfg.db_path))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(cfg.vault_path))
    archive = tmp_path / "cli.tar.gz"

    rc = cli_main(["backup", str(archive)])
    assert rc == 0
    assert archive.is_file()

    cfg.db_path.unlink()
    import shutil
    shutil.rmtree(cfg.vault_path)

    rc = cli_main(["restore", str(archive)])
    assert rc == 0
    after = _snapshot_acceptance_sql(cfg.db_path, seeded)
    assert after == before


def test_cli_restore_no_verify_flag(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    _seed(cfg)
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)
    tampered = tmp_path / "tampered.tar.gz"
    _repack_with_tampered_db(archive, tampered, mutate_db=lambda _b: b"\0")

    monkeypatch.setenv("PARALLAX_DB_PATH", str(cfg.db_path))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(cfg.vault_path))
    cfg.db_path.unlink()

    rc_fail = cli_main(["restore", str(tampered)])
    assert rc_fail == 3

    # Re-extract the tampered archive but skip verification.
    cfg.db_path.unlink(missing_ok=True)
    rc_pass = cli_main(["restore", str(tampered), "--no-verify"])
    assert rc_pass == 0


def test_cli_no_args_returns_usage_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main([])
    assert rc == 2


def test_manifest_round_trip() -> None:
    m = BackupManifest(
        parallax_version="0.2.1",
        schema_version=5,
        created_at="2026-04-18T00:00:00+00:00",
        db_sha256="a" * 64,
        row_counts={"memories": 1, "claims": 1},
        content_hash_counts={"memories_hash_count": 1, "claims_hash_count": 1},
    )
    assert BackupManifest.from_dict(m.to_dict()) == m
    # JSON round-trip for safety.
    assert BackupManifest.from_dict(json.loads(json.dumps(m.to_dict()))) == m
