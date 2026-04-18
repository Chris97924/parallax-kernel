"""``parallax`` command-line entry point.

Two subcommands:

    parallax backup  <archive.tar.gz>
    parallax restore <archive.tar.gz> [--no-verify]

Reads runtime paths from :func:`parallax.config.load_config` so the same
``PARALLAX_DB_PATH`` / ``PARALLAX_VAULT_PATH`` env vars that drive the rest
of the library also drive backup/restore.

Exit codes
----------
* 0 -- success
* 1 -- user-visible error (missing file, archive already exists, etc.)
* 2 -- argparse usage error (unknown/missing subcommand)
* 3 -- restore verification mismatch (reserved for
        :class:`parallax.restore.RestoreVerificationError`)
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence

from parallax.backup import create_backup
from parallax.config import load_config
from parallax.restore import RestoreVerificationError, restore_backup

__all__ = ["main", "build_parser"]

_EXIT_OK = 0
_EXIT_USER_ERROR = 1
_EXIT_USAGE = 2
_EXIT_VERIFY_FAIL = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parallax",
        description="Parallax Kernel CLI — backup / restore the canonical store.",
    )
    sub = parser.add_subparsers(dest="command", metavar="{backup,restore}")

    p_backup = sub.add_parser("backup", help="Write a tar.gz backup archive.")
    p_backup.add_argument("archive", type=pathlib.Path, help="destination .tar.gz path")

    p_restore = sub.add_parser("restore", help="Restore from a tar.gz backup archive.")
    p_restore.add_argument("archive", type=pathlib.Path, help="source .tar.gz path")
    p_restore.add_argument(
        "--no-verify",
        action="store_true",
        help="skip manifest verification after restore (default: verify)",
    )
    return parser


def _cmd_backup(archive: pathlib.Path) -> int:
    cfg = load_config()
    try:
        manifest = create_backup(cfg, archive)
    except (FileNotFoundError, FileExistsError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return _EXIT_USER_ERROR
    print(
        f"Wrote backup: {archive}\n"
        f"  db_sha256: {manifest.db_sha256}\n"
        f"  row_counts: {manifest.row_counts}"
    )
    return _EXIT_OK


def _cmd_restore(archive: pathlib.Path, *, verify: bool) -> int:
    cfg = load_config()
    try:
        manifest = restore_backup(cfg, archive, verify=verify)
    except FileNotFoundError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return _EXIT_USER_ERROR
    except RestoreVerificationError as exc:
        print(f"restore verification failed:\n{exc}", file=sys.stderr)
        return _EXIT_VERIFY_FAIL
    print(
        f"Restored from: {archive}\n"
        f"  verified: {verify}\n"
        f"  row_counts: {manifest.row_counts}"
    )
    return _EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "backup":
        return _cmd_backup(args.archive)
    if args.command == "restore":
        return _cmd_restore(args.archive, verify=not args.no_verify)
    parser.print_help(sys.stderr)
    return _EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
