"""Restore contract for Parallax (v0.2.1, Step 2).

Extracts an archive produced by :func:`parallax.backup.create_backup`,
installs the db + vault into the paths named by a
:class:`parallax.config.ParallaxConfig`, and (by default) verifies the
restored db against the archive's manifest.

Safety contract
---------------
* Extraction uses a manual safe-entry filter that rejects absolute paths,
  parent-directory traversal (``..``), and anything that is not a regular
  file or directory. This keeps us portable to Python 3.11 (where
  ``tarfile.data_filter`` isn't available) while still refusing the
  classic tar-traversal attack.
* Any pre-existing db / vault target is moved aside to ``<path>.bak-<ts>``
  BEFORE the archive contents are installed. Restore therefore never
  silently destroys the operator's current state.
* Verification re-opens the restored db, recomputes row counts +
  content_hash counts + file sha256, and raises
  :class:`RestoreVerificationError` on any drift.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import shutil
import sqlite3
import tarfile
import tempfile
from typing import Any

from parallax.backup import (
    MANIFEST_NAME,
    BackupManifest,
    compute_manifest_from_db,
)

__all__ = ["RestoreVerificationError", "restore_backup"]

_DB_ARCHIVE_PATH = "db/parallax.db"
_VAULT_PREFIX = "vault"


class RestoreVerificationError(Exception):
    """Raised when the restored db disagrees with its manifest."""


def _timestamp() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%S")


def _is_safe_member(member: tarfile.TarInfo, dest_root: pathlib.Path) -> bool:
    """Reject absolute paths, parent traversal, and special files."""
    name = member.name.replace("\\", "/")
    if name.startswith("/") or ":" in name.split("/", 1)[0]:
        return False
    resolved = (dest_root / name).resolve()
    try:
        resolved.relative_to(dest_root.resolve())
    except ValueError:
        return False
    if not (member.isfile() or member.isdir()):
        return False
    return True


def _safe_extract(tar: tarfile.TarFile, dest: pathlib.Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for member in tar.getmembers():
        if not _is_safe_member(member, dest):
            raise tarfile.TarError(f"unsafe archive entry rejected: {member.name!r}")
    tar.extractall(path=str(dest))


def _move_aside(target: pathlib.Path) -> pathlib.Path | None:
    """If ``target`` exists, rename to ``<target>.bak-<ts>`` and return the backup path."""
    if not target.exists():
        return None
    suffix = f".bak-{_timestamp()}"
    bak = target.with_name(target.name + suffix)
    # Collision-proof the suffix even when the same second is hit twice.
    n = 1
    while bak.exists():
        bak = target.with_name(target.name + suffix + f"-{n}")
        n += 1
    target.rename(bak)
    return bak


def _diff_manifests(expected: BackupManifest, actual: BackupManifest) -> list[str]:
    diffs: list[str] = []
    if expected.db_sha256 != actual.db_sha256:
        diffs.append(
            f"db_sha256 mismatch: expected={expected.db_sha256} actual={actual.db_sha256}"
        )
    for table, want in expected.row_counts.items():
        got = actual.row_counts.get(table)
        if got != want:
            diffs.append(f"row_counts[{table}] mismatch: expected={want} actual={got}")
    for key, want in expected.content_hash_counts.items():
        got = actual.content_hash_counts.get(key)
        if got != want:
            diffs.append(
                f"content_hash_counts[{key}] mismatch: expected={want} actual={got}"
            )
    return diffs


def restore_backup(
    cfg: Any,
    archive_path: pathlib.Path,
    *,
    verify: bool = True,
) -> BackupManifest:
    """Extract + install + optionally verify a backup archive.

    Returns the manifest that was stored inside the archive.
    """
    archive_path = pathlib.Path(archive_path)
    db_path = pathlib.Path(cfg.db_path)
    vault_path = pathlib.Path(cfg.vault_path)

    if not archive_path.is_file():
        raise FileNotFoundError(f"archive not found: {archive_path}")

    with tempfile.TemporaryDirectory(prefix="parallax-restore-") as staging_str:
        staging = pathlib.Path(staging_str)
        with tarfile.open(archive_path, "r:gz") as tar:
            _safe_extract(tar, staging)

        manifest_file = staging / MANIFEST_NAME
        if not manifest_file.is_file():
            raise tarfile.TarError(
                f"archive missing {MANIFEST_NAME}: {archive_path}"
            )
        expected = BackupManifest.from_dict(json.loads(manifest_file.read_text("utf-8")))

        staged_db = staging / _DB_ARCHIVE_PATH
        if not staged_db.is_file():
            raise tarfile.TarError(
                f"archive missing {_DB_ARCHIVE_PATH}: {archive_path}"
            )

        db_path.parent.mkdir(parents=True, exist_ok=True)
        _move_aside(db_path)
        shutil.move(str(staged_db), str(db_path))

        staged_vault = staging / _VAULT_PREFIX
        if staged_vault.is_dir():
            _move_aside(vault_path)
            vault_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged_vault), str(vault_path))

    if verify:
        try:
            actual = compute_manifest_from_db(
                db_path,
                parallax_version=expected.parallax_version,
                created_at=expected.created_at,
            )
        except sqlite3.DatabaseError as exc:
            raise RestoreVerificationError(
                f"restored db is not a valid SQLite database "
                f"(db_sha256 check could not even be reached): {exc}"
            ) from exc
        diffs = _diff_manifests(expected, actual)
        if diffs:
            raise RestoreVerificationError(
                "restore verification failed:\n  " + "\n  ".join(diffs)
            )

    return expected
