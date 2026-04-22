"""Backup contract for Parallax (v0.2.1, Step 2).

Bundles the SQLite canonical store + the vault directory + a verifiable
manifest into a single .tar.gz. Zero non-stdlib dependencies: uses
``tarfile`` + ``hashlib`` + ``json`` + ``sqlite3`` only so ``parallax
backup`` works from a minimal install.

Contract
--------
1. Open cfg.db_path, run ``PRAGMA wal_checkpoint(TRUNCATE)`` — stale WAL
   pages would otherwise make the copied main db file inconsistent.
   Abort with ``RuntimeError`` if the checkpoint result's first element
   is non-zero (SQLite returns ``(0, <log>, <checkpointed>)`` on success).
2. Close the connection, then write a tar.gz containing:
     ``db/parallax.db``    -- byte copy of the checkpointed db file
     ``vault/**``          -- recursive copy of cfg.vault_path (if present)
     ``manifest.json``     -- :class:`BackupManifest` serialised
3. The manifest's ``db_sha256`` is the digest of the copied db bytes,
   giving :func:`parallax.restore.restore_backup` a single-file integrity
   target without re-hashing every table.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import io
import json
import pathlib
import sqlite3
import tarfile
from typing import Any

__all__ = ["BackupManifest", "MANIFEST_NAME", "create_backup", "upload_to", "download_from"]

MANIFEST_NAME = "manifest.json"
_DB_ARCHIVE_PATH = "db/parallax.db"
_VAULT_PREFIX = "vault"
_COUNTED_TABLES = (
    "sources",
    "memories",
    "claims",
    "decisions",
    "events",
    "index_state",
)


@dataclasses.dataclass(frozen=True)
class BackupManifest:
    """Verifiable summary of a Parallax backup.

    Written as ``manifest.json`` inside the archive and re-computed on
    restore. Every field must round-trip: :func:`from_dict` ∘ :func:`to_dict`
    is the identity on well-formed dicts.
    """

    parallax_version: str
    schema_version: int
    created_at: str
    db_sha256: str
    row_counts: dict[str, int]
    content_hash_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "parallax_version": self.parallax_version,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "db_sha256": self.db_sha256,
            "row_counts": dict(self.row_counts),
            "content_hash_counts": dict(self.content_hash_counts),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupManifest:
        return cls(
            parallax_version=str(data["parallax_version"]),
            schema_version=int(data["schema_version"]),
            created_at=str(data["created_at"]),
            db_sha256=str(data["db_sha256"]),
            row_counts={str(k): int(v) for k, v in dict(data["row_counts"]).items()},
            content_hash_counts={
                str(k): int(v) for k, v in dict(data["content_hash_counts"]).items()
            },
        )


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    return int(row[0])


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _COUNTED_TABLES:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = int(row[0])
    return counts


def _content_hash_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "memories_hash_count": int(
            conn.execute("SELECT COUNT(DISTINCT content_hash) FROM memories").fetchone()[0]
        ),
        "claims_hash_count": int(
            conn.execute("SELECT COUNT(DISTINCT content_hash) FROM claims").fetchone()[0]
        ),
    }


def compute_manifest_from_db(
    db_path: pathlib.Path,
    *,
    parallax_version: str,
    created_at: str | None = None,
) -> BackupManifest:
    """Build a :class:`BackupManifest` by reading a live (or restored) db."""
    conn = sqlite3.connect(str(db_path))
    try:
        schema_version = _schema_version(conn)
        row_counts = _row_counts(conn)
        content_hash_counts = _content_hash_counts(conn)
    finally:
        conn.close()
    return BackupManifest(
        parallax_version=parallax_version,
        schema_version=schema_version,
        created_at=created_at or _dt.datetime.now(_dt.UTC).isoformat(),
        db_sha256=_sha256_file(db_path),
        row_counts=row_counts,
        content_hash_counts=content_hash_counts,
    )


def _checkpoint_truncate(db_path: pathlib.Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError("PRAGMA wal_checkpoint returned no row")
    if int(row[0]) != 0:
        raise RuntimeError(
            f"PRAGMA wal_checkpoint(TRUNCATE) failed: result={tuple(row)}"
        )


def create_backup(cfg: Any, archive_path: pathlib.Path) -> BackupManifest:
    """Write a tar.gz backup of cfg.db_path + cfg.vault_path + manifest.json.

    ``cfg`` is duck-typed on ``db_path`` / ``vault_path`` so tests can pass a
    plain dataclass instead of the full :class:`ParallaxConfig`. Raises
    FileNotFoundError (db missing), FileExistsError (archive already exists —
    silent clobbering is refused), RuntimeError (WAL checkpoint failed).
    """
    db_path = pathlib.Path(cfg.db_path)
    vault_path = pathlib.Path(cfg.vault_path)
    archive_path = pathlib.Path(archive_path)

    if not db_path.is_file():
        raise FileNotFoundError(f"cfg.db_path does not exist: {db_path}")
    if archive_path.exists():
        raise FileExistsError(f"archive already exists: {archive_path}")

    _checkpoint_truncate(db_path)

    # Defer the version import until runtime so circular imports stay
    # impossible — backup.py must not depend on __init__'s re-export web.
    from parallax import __version__ as parallax_version

    manifest = compute_manifest_from_db(
        db_path,
        parallax_version=parallax_version,
    )

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(str(db_path), arcname=_DB_ARCHIVE_PATH)
        if vault_path.is_dir():
            tar.add(str(vault_path), arcname=_VAULT_PREFIX)
        manifest_bytes = json.dumps(
            manifest.to_dict(), sort_keys=True, indent=2
        ).encode("utf-8")
        info = tarfile.TarInfo(name=MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mtime = int(_dt.datetime.now(_dt.UTC).timestamp())
        tar.addfile(info, io.BytesIO(manifest_bytes))

    return manifest


# ---------------------------------------------------------------------------
# Cloud upload / download helpers (optional boto3 dep)
# ---------------------------------------------------------------------------

def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse ``s3://bucket/key`` → ``(bucket, key)``. Raises ValueError on bad input."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri!r}")
    rest = uri[len("s3://"):]
    if "/" not in rest:
        raise ValueError(f"s3:// URI must have format s3://bucket/key, got {uri!r}")
    bucket, key = rest.split("/", 1)
    if not bucket or not key:
        raise ValueError(f"s3:// URI must have non-empty bucket and key, got {uri!r}")
    return bucket, key


def _get_boto3_client():
    """Return a boto3 S3 client, raising ImportError with a helpful message if boto3 is absent."""
    try:
        import boto3  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "boto3 is required for s3:// destinations. "
            "Install it with: pip install 'parallax-kernel[cloud]'"
        ) from exc
    import os
    endpoint_url = os.environ.get("AWS_ENDPOINT_URL") or None
    return boto3.client("s3", endpoint_url=endpoint_url)


def upload_to(archive_path: pathlib.Path, destination_uri: str) -> None:
    """Upload *archive_path* to *destination_uri*.

    If *destination_uri* starts with ``s3://`` the archive is uploaded to S3
    (or any S3-compatible service) using boto3. Otherwise the function copies
    the archive to *destination_uri* treated as a local filesystem path.

    Raises
    ------
    ImportError
        boto3 not installed and an ``s3://`` destination was requested.
    ValueError
        Malformed ``s3://`` URI.
    FileNotFoundError
        *archive_path* does not exist.
    """
    archive_path = pathlib.Path(archive_path)
    if not archive_path.is_file():
        raise FileNotFoundError(f"archive not found: {archive_path}")

    if destination_uri.startswith("s3://"):
        bucket, key = _parse_s3_uri(destination_uri)
        client = _get_boto3_client()
        client.upload_file(str(archive_path), bucket, key)
    else:
        import shutil
        dest = pathlib.Path(destination_uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(archive_path), str(dest))


def download_from(source_uri: str, dest_path: pathlib.Path) -> None:
    """Download an archive from *source_uri* to *dest_path*.

    If *source_uri* starts with ``s3://`` the archive is downloaded from S3
    (or any S3-compatible service) using boto3. Otherwise the file at
    *source_uri* (local path) is copied to *dest_path*.

    Raises
    ------
    ImportError
        boto3 not installed and an ``s3://`` source was requested.
    ValueError
        Malformed ``s3://`` URI.
    """
    dest_path = pathlib.Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if source_uri.startswith("s3://"):
        bucket, key = _parse_s3_uri(source_uri)
        client = _get_boto3_client()
        client.download_file(bucket, key, str(dest_path))
    else:
        import shutil
        shutil.copy2(source_uri, str(dest_path))
