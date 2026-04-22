"""Cloud backup/restore tests for Parallax (B6).

Uses moto to mock AWS S3 so no real credentials or network are required.
Verifies that upload_to / download_from produce a bit-identical round-trip,
and that the CLI --to / --from flags work end-to-end.
"""

from __future__ import annotations

import dataclasses
import pathlib
import tarfile

import boto3
import pytest

# moto must be imported before boto3 is used so the interceptor is active.
from moto import mock_aws

from parallax.backup import (
    create_backup,
    download_from,
    upload_to,
)
from parallax.cli import main as cli_main
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect

# ---------------------------------------------------------------------------
# Minimal cfg fixture (mirrors test_backup_restore.py pattern)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class _Cfg:
    db_path: pathlib.Path
    vault_path: pathlib.Path


@pytest.fixture()
def cfg(tmp_path: pathlib.Path) -> _Cfg:
    db = tmp_path / "parallax.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    conn = connect(str(db))
    migrate_to_latest(conn)
    conn.close()
    return _Cfg(db_path=db, vault_path=vault)


# ---------------------------------------------------------------------------
# S3 fixtures
# ---------------------------------------------------------------------------

_BUCKET = "test-parallax-backup"
_KEY = "backups/test.tar.gz"
_S3_URI = f"s3://{_BUCKET}/{_KEY}"


@pytest.fixture()
def s3_bucket():
    """Spin up a moto-mocked S3 bucket for the duration of a test."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET)
        yield client


# ---------------------------------------------------------------------------
# Unit: upload_to / download_from round-trip
# ---------------------------------------------------------------------------

def test_upload_download_roundtrip(cfg: _Cfg, tmp_path: pathlib.Path, s3_bucket):
    """Bit-identical round-trip: create archive → upload → download → compare."""
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)

    original_bytes = archive.read_bytes()

    # Upload
    upload_to(archive, _S3_URI)

    # Confirm object exists in the mock bucket
    response = s3_bucket.get_object(Bucket=_BUCKET, Key=_KEY)
    s3_bytes = response["Body"].read()
    assert s3_bytes == original_bytes, "S3 object content differs from local archive"

    # Download to a different path
    downloaded = tmp_path / "downloaded.tar.gz"
    download_from(_S3_URI, downloaded)

    assert downloaded.read_bytes() == original_bytes, (
        "Downloaded bytes differ from original"
    )


def test_upload_download_archive_is_valid_tar(
    cfg: _Cfg, tmp_path: pathlib.Path, s3_bucket
):
    """Downloaded archive must open as a valid tar.gz with manifest.json inside."""
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)
    upload_to(archive, _S3_URI)

    downloaded = tmp_path / "downloaded.tar.gz"
    download_from(_S3_URI, downloaded)

    with tarfile.open(downloaded, "r:gz") as tf:
        names = tf.getnames()
    assert "manifest.json" in names


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_upload_missing_archive_raises(tmp_path: pathlib.Path, s3_bucket):
    missing = tmp_path / "nonexistent.tar.gz"
    with pytest.raises(FileNotFoundError):
        upload_to(missing, _S3_URI)


def test_bad_s3_uri_raises_value_error(tmp_path: pathlib.Path, cfg: _Cfg, s3_bucket):
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)
    with pytest.raises(ValueError, match="s3://"):
        upload_to(archive, "s3://bucket-only-no-key")


def test_upload_to_local_path(cfg: _Cfg, tmp_path: pathlib.Path):
    """upload_to with a local destination path should copy the file."""
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)

    dest = tmp_path / "copy" / "backup_copy.tar.gz"
    upload_to(archive, str(dest))

    assert dest.exists()
    assert dest.read_bytes() == archive.read_bytes()


def test_download_from_local_path(cfg: _Cfg, tmp_path: pathlib.Path):
    """download_from with a local source path should copy the file."""
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)

    dest = tmp_path / "fetched.tar.gz"
    download_from(str(archive), dest)

    assert dest.read_bytes() == archive.read_bytes()


# ---------------------------------------------------------------------------
# CLI integration: --to and --from flags
# ---------------------------------------------------------------------------

def test_cli_backup_to_s3(cfg: _Cfg, tmp_path: pathlib.Path, s3_bucket, monkeypatch):
    """``parallax backup archive.tar.gz --to s3://...`` uploads and removes local."""
    monkeypatch.setenv("PARALLAX_DB_PATH", str(cfg.db_path))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(cfg.vault_path))

    archive = tmp_path / "backup.tar.gz"
    rc = cli_main(["backup", str(archive), "--to", _S3_URI])

    assert rc == 0
    # Local tmp archive should be removed after upload
    assert not archive.exists(), "local archive should be removed after upload"
    # Object must exist in mock S3
    resp = s3_bucket.get_object(Bucket=_BUCKET, Key=_KEY)
    assert resp["Body"].read()  # non-empty


def test_cli_restore_from_s3(cfg: _Cfg, tmp_path: pathlib.Path, s3_bucket, monkeypatch):
    """``parallax restore archive.tar.gz --from s3://...`` downloads then restores."""
    monkeypatch.setenv("PARALLAX_DB_PATH", str(cfg.db_path))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(cfg.vault_path))

    # First create + upload an archive
    archive = tmp_path / "backup.tar.gz"
    create_backup(cfg, archive)
    upload_to(archive, _S3_URI)
    archive.unlink()  # simulate it being gone locally

    # Now restore via CLI using --from
    restore_dest = tmp_path / "restore.tar.gz"
    rc = cli_main(["restore", str(restore_dest), "--from", _S3_URI, "--no-verify"])

    assert rc == 0


def test_cli_backup_upload_import_error(
    cfg: _Cfg, tmp_path: pathlib.Path, monkeypatch
):
    """When boto3 is absent, --to s3:// should exit with code 1."""
    monkeypatch.setenv("PARALLAX_DB_PATH", str(cfg.db_path))
    monkeypatch.setenv("PARALLAX_VAULT_PATH", str(cfg.vault_path))

    # Simulate boto3 not installed by patching upload_to to raise ImportError
    import parallax.cli as cli_mod

    def _raise(*a, **kw):
        raise ImportError("boto3 not installed")

    monkeypatch.setattr(cli_mod, "upload_to", _raise)

    archive = tmp_path / "backup.tar.gz"
    rc = cli_main(["backup", str(archive), "--to", _S3_URI])
    assert rc == 1
