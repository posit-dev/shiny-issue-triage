"""triage-hub command-line interface."""

from __future__ import annotations

import argparse
import pathlib

from . import config, db
from . import sync as sync_mod
from . import snapshot as snapshot_mod

DEFAULT_DB = ".data/mirror.sqlite"
DEFAULT_CONFIG = "config/repos.yaml"


def _open_db(path: str) -> "db.sqlite3.Connection":
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    return db.connect(path)


def _cmd_sync(args: argparse.Namespace) -> int:
    repos = [r.full for r in config.load_repos(args.config)]
    if args.repo:
        if args.repo not in repos:
            print(f"error: {args.repo} is not in {args.config}")
            return 1
        repos = [args.repo]
    con = _open_db(args.db)
    totals = sync_mod.sync_all(con, repos, full=args.full, log=print)
    print(f"synced {totals['repos']} repos: {totals['issues']} issues, "
          f"{totals['prs']} PRs, {totals['comments']} comments")
    return 0


def _cmd_snapshot_publish(args: argparse.Namespace) -> int:
    tag = snapshot_mod.publish(args.db, dated=args.dated)
    print(f"published snapshot to release {tag} (and {snapshot_mod.LATEST_TAG})")
    return 0


def _cmd_snapshot_bootstrap(args: argparse.Namespace) -> int:
    snapshot_mod.bootstrap(args.db, force=args.force)
    print(f"bootstrapped {args.db} from {snapshot_mod.LATEST_TAG}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triage-hub")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="mirror issues/PRs/comments to SQLite")
    p_sync.add_argument("--db", default=DEFAULT_DB)
    p_sync.add_argument("--config", default=DEFAULT_CONFIG)
    p_sync.add_argument("--repo", help="sync only this owner/name")
    p_sync.add_argument("--full", action="store_true",
                        help="ignore cursors and re-walk everything")
    p_sync.set_defaults(func=_cmd_sync)

    p_snap = sub.add_parser("snapshot", help="publish or fetch mirror snapshots")
    snap_sub = p_snap.add_subparsers(dest="snapshot_command", required=True)

    p_pub = snap_sub.add_parser("publish")
    p_pub.add_argument("--db", default=DEFAULT_DB)
    p_pub.add_argument("--dated", action="store_true",
                       help="also cut a dated mirror-YYYY-MM-DD restore point")
    p_pub.set_defaults(func=_cmd_snapshot_publish)

    p_boot = snap_sub.add_parser("bootstrap")
    p_boot.add_argument("--db", default=DEFAULT_DB)
    p_boot.add_argument("--force", action="store_true")
    p_boot.set_defaults(func=_cmd_snapshot_bootstrap)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
