"""``parallax`` command-line entry point.

Subcommands:

    parallax backup  <archive.tar.gz>
    parallax restore <archive.tar.gz> [--no-verify]
    parallax inspect events   [--session <id>] [--limit N]
    parallax inspect retrieve "<query>" [--explain] [--level N] [--kind KIND]
    parallax inspect inject   [--session <id>] [--max N]

Reads runtime paths from :func:`parallax.config.load_config` so the same
``PARALLAX_DB_PATH`` / ``PARALLAX_VAULT_PATH`` env vars that drive the rest
of the library also drive the CLI.

Exit codes
----------
* 0 -- success
* 1 -- user-visible error (missing file, archive already exists, bad
        session/query/kind, etc.)
* 2 -- argparse usage error (unknown/missing subcommand)
* 3 -- restore verification mismatch
        (:class:`parallax.restore.RestoreVerificationError`).
"""

from __future__ import annotations

import argparse
import os
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

_RETRIEVE_KINDS = {"recent", "file", "decision", "bug", "entity", "timeline"}


def _default_user() -> str:
    return os.environ.get("PARALLAX_USER_ID", "chris")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parallax",
        description="Parallax Kernel CLI — backup / restore / inspect the canonical store.",
    )
    sub = parser.add_subparsers(dest="command", metavar="{backup,restore,inspect}")

    p_backup = sub.add_parser("backup", help="Write a tar.gz backup archive.")
    p_backup.add_argument("archive", type=pathlib.Path, help="destination .tar.gz path")

    p_restore = sub.add_parser("restore", help="Restore from a tar.gz backup archive.")
    p_restore.add_argument("archive", type=pathlib.Path, help="source .tar.gz path")
    p_restore.add_argument(
        "--no-verify",
        action="store_true",
        help="skip manifest verification after restore (default: verify)",
    )

    p_inspect = sub.add_parser(
        "inspect", help="Inspect events and retrieval hits for debugging."
    )
    p_inspect.add_argument(
        "--user-id",
        default=None,
        help="user_id to scope queries to (defaults to $PARALLAX_USER_ID or 'chris')",
    )
    isub = p_inspect.add_subparsers(dest="inspect_cmd", metavar="{events,retrieve,inject}")

    p_events = isub.add_parser("events", help="List events for a session.")
    p_events.add_argument("--session", dest="session_id", default=None)
    p_events.add_argument("--limit", type=int, default=20)

    p_retr = isub.add_parser("retrieve", help="Run a retrieval API call and print hits.")
    p_retr.add_argument("query", nargs="?", default="")
    p_retr.add_argument("--explain", action="store_true")
    p_retr.add_argument("--level", type=int, default=1, choices=[1, 2, 3])
    p_retr.add_argument(
        "--kind",
        default=None,
        help=f"retrieval kind; one of {sorted(_RETRIEVE_KINDS)}",
    )
    p_retr.add_argument("--limit", type=int, default=10)
    p_retr.add_argument("--since", default=None)
    p_retr.add_argument("--until", default=None)

    p_inject = isub.add_parser(
        "inject", help="Print a SessionStart <system-reminder> block."
    )
    p_inject.add_argument("--session", dest="session_id", default=None)
    p_inject.add_argument("--max", dest="max_hits", type=int, default=8)

    p_mig = isub.add_parser(
        "migrate",
        help="Show the migration plan (non-destructive).",
    )
    p_mig.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit flag; dry-run is the only mode this subcommand supports",
    )
    p_mig.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the migration plan as JSON for machine consumption",
    )

    return parser


# ----- backup / restore -----------------------------------------------------


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


# ----- inspect --------------------------------------------------------------


def _open_conn():
    cfg = load_config()
    from parallax.sqlite_store import connect
    return connect(cfg.db_path)


def _cmd_inspect_events(*, user_id: str, session_id: str | None, limit: int) -> int:
    conn = _open_conn()
    try:
        if session_id is None:
            rows = conn.execute(
                "SELECT session_id, COUNT(*) AS n FROM events "
                "WHERE user_id = ? AND session_id IS NOT NULL "
                "GROUP BY session_id ORDER BY MAX(created_at) DESC LIMIT 5",
                (user_id,),
            ).fetchall()
            if not rows:
                print("(no sessions found)")
                return _EXIT_OK
            print("Recent sessions:")
            for r in rows:
                print(f"  {r['session_id']}  events={r['n']}")
            return _EXIT_OK

        rows = conn.execute(
            "SELECT created_at, event_type, target_kind, target_id, payload_json "
            "FROM events WHERE user_id = ? AND session_id = ? "
            "ORDER BY created_at ASC LIMIT ?",
            (user_id, session_id, limit),
        ).fetchall()
        if not rows:
            print(f"no events for session {session_id!r}", file=sys.stderr)
            return _EXIT_USER_ERROR
        for r in rows:
            summary = (r["payload_json"] or "")[:120].replace("\n", " ")
            tk = r["target_kind"] or "-"
            ti = r["target_id"] or "-"
            print(f"{r['created_at']}  {r['event_type']:<22}  {tk}:{ti}  {summary}")
        return _EXIT_OK
    finally:
        conn.close()


def _pick_retrieve_kind(query: str, kind: str | None) -> str:
    if kind is not None:
        return kind
    return "recent" if not query else "entity"


def _cmd_inspect_retrieve(
    *,
    user_id: str,
    query: str,
    kind: str | None,
    explain: bool,
    level: int,
    limit: int,
    since: str | None,
    until: str | None,
) -> int:
    from parallax import retrieve as R

    resolved_kind = _pick_retrieve_kind(query, kind)
    if resolved_kind not in _RETRIEVE_KINDS:
        print(
            f"unknown --kind {resolved_kind!r}; expected one of {sorted(_RETRIEVE_KINDS)}",
            file=sys.stderr,
        )
        return _EXIT_USER_ERROR
    conn = _open_conn()
    try:
        if resolved_kind == "recent":
            hits = R.recent_context(conn, user_id=user_id, limit=limit)
        elif resolved_kind == "file":
            hits = R.by_file(conn, user_id=user_id, path=query, limit=limit)
        elif resolved_kind == "decision":
            hits = R.by_decision(conn, user_id=user_id, limit=limit)
        elif resolved_kind == "bug":
            hits = R.by_bug_fix(conn, user_id=user_id, limit=limit)
        elif resolved_kind == "entity":
            hits = R.by_entity(conn, user_id=user_id, subject=query, limit=limit)
        elif resolved_kind == "timeline":
            if since is None or until is None:
                print("timeline kind requires --since and --until", file=sys.stderr)
                return _EXIT_USER_ERROR
            try:
                hits = R.by_timeline(
                    conn, user_id=user_id, since=since, until=until, limit=limit
                )
            except ValueError as exc:
                print(f"bad timeline window: {exc}", file=sys.stderr)
                return _EXIT_USER_ERROR
        else:  # pragma: no cover — guarded by _RETRIEVE_KINDS check
            return _EXIT_USER_ERROR

        if not hits:
            print("(no hits)")
            return _EXIT_OK

        for h in hits:
            proj = h.project(level)
            print(
                f"[{proj['entity_kind']}:{proj['entity_id']}] "
                f"score={proj['score']:.3f}  {proj['title']}"
            )
            if level >= 2 and proj.get("evidence"):
                print(f"    evidence: {proj['evidence']}")
            if level >= 3:
                print(f"    full: {proj.get('full')}")
            if explain:
                print(f"    reason: {h.explain.get('reason','')}")
                print(f"    score_components: {h.explain.get('score_components', {})}")
        return _EXIT_OK
    finally:
        conn.close()


def _cmd_inspect_migrate(*, as_json: bool) -> int:
    import json as _json

    from parallax.migrations import MIGRATIONS, migration_plan

    conn = _open_conn()
    try:
        plan = migration_plan(conn)
    finally:
        conn.close()

    if as_json:
        payload = {
            "applied": list(plan.applied),
            "current_version": plan.current_version,
            "target_version": plan.target_version,
            "pending": [
                {
                    "version": s.version,
                    "name": s.name,
                    "statements": list(s.statements),
                    "row_impact_estimates": s.row_impact_estimates,
                }
                for s in plan.pending
            ],
        }
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return _EXIT_OK

    applied_set = set(plan.applied)
    print(f"{'version':>7}  {'name':<26}  {'status':<8}  est_row_impact")
    for mig in sorted(MIGRATIONS, key=lambda m: m.version):
        if mig.version in applied_set:
            status = "applied"
            impact = "-"
        else:
            status = "pending"
            step = next(
                (s for s in plan.pending if s.version == mig.version), None
            )
            impact = (
                ",".join(
                    f"{t}={n}" for t, n in sorted(step.row_impact_estimates.items())
                )
                if step
                else "-"
            )
        print(f"{mig.version:>7}  {mig.name:<26}  {status:<8}  {impact}")
    return _EXIT_OK


def _cmd_inspect_inject(*, user_id: str, session_id: str | None, max_hits: int) -> int:
    from parallax.injector import build_session_reminder

    conn = _open_conn()
    try:
        text = build_session_reminder(
            conn, user_id=user_id, session_id=session_id, max_hits=max_hits
        )
        print(text)
        return _EXIT_OK
    finally:
        conn.close()


# ----- dispatcher -----------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "backup":
        return _cmd_backup(args.archive)
    if args.command == "restore":
        return _cmd_restore(args.archive, verify=not args.no_verify)
    if args.command == "inspect":
        user_id = args.user_id if args.user_id is not None else _default_user()
        if args.inspect_cmd == "events":
            return _cmd_inspect_events(
                user_id=user_id,
                session_id=args.session_id,
                limit=args.limit,
            )
        if args.inspect_cmd == "retrieve":
            return _cmd_inspect_retrieve(
                user_id=user_id,
                query=args.query,
                kind=args.kind,
                explain=args.explain,
                level=args.level,
                limit=args.limit,
                since=args.since,
                until=args.until,
            )
        if args.inspect_cmd == "inject":
            return _cmd_inspect_inject(
                user_id=user_id,
                session_id=args.session_id,
                max_hits=args.max_hits,
            )
        if args.inspect_cmd == "migrate":
            return _cmd_inspect_migrate(as_json=args.as_json)
        parser.parse_args(["inspect", "--help"])
        return _EXIT_USAGE
    parser.print_help(sys.stderr)
    return _EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
