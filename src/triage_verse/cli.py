"""triage-verse command-line interface."""

from __future__ import annotations

import argparse
import pathlib

from . import analytics as analytics_mod
from . import analyze as analyze_mod
from . import config, db
from . import embed as embed_mod
from . import llm
from . import sync as sync_mod
from . import snapshot as snapshot_mod
from . import verify as verify_mod

DEFAULT_DB = ".data/mirror.sqlite"
DEFAULT_CONFIG = "config/repos.yaml"
DEFAULT_MODELS = "config/models.yaml"
DEFAULT_PROPOSALS = ".data/proposals"


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
    print(
        f"synced {totals['repos']} repos: {totals['issues']} issues, "
        f"{totals['prs']} PRs, {totals['comments']} comments"
    )
    return 0


def _cmd_snapshot_publish(args: argparse.Namespace) -> int:
    tag = snapshot_mod.publish(args.db, dated=args.dated)
    if tag == snapshot_mod.LATEST_TAG:
        print(f"published snapshot to release {tag}")
    else:
        print(f"published snapshot to releases {tag} and {snapshot_mod.LATEST_TAG}")
    return 0


def _cmd_snapshot_bootstrap(args: argparse.Namespace) -> int:
    snapshot_mod.bootstrap(args.db, force=args.force)
    print(f"bootstrapped {args.db} from {snapshot_mod.LATEST_TAG}")
    return 0


def _cmd_analytics_export(args: argparse.Namespace) -> int:
    con = _open_db(args.db)
    analytics_mod.export(con, args.out)
    print(f"wrote {args.out}")
    return 0


def _cmd_verify_counts(args: argparse.Namespace) -> int:
    repos = [r.full for r in config.load_repos(args.config)]
    con = _open_db(args.db)
    results = verify_mod.verify_counts(con, repos, tolerance=args.tolerance)
    bad = [r for r in results if not r["ok"]]
    for r in results:
        flag = "OK " if r["ok"] else "MISMATCH"
        diff = r["github"] - r["mirror"]
        print(
            f"{flag} {r['repo']}: mirror={r['mirror']} "
            f"github={r['github']} diff={diff:+d}"
        )
    print(f"{len(results) - len(bad)}/{len(results)} repos reconcile")
    return 1 if bad else 0


def _cmd_embed(args: argparse.Namespace) -> int:
    cfg = config.load_models_config(args.models_config)
    repos = [r.full for r in config.load_repos(args.config)]
    if args.repo:
        repos = [args.repo]
    con = _open_db(args.db)
    embedder = embed_mod.FastEmbedEmbedder(cfg.embed_model)
    total = sum(embed_mod.embed_repo(con, r, embedder, full=args.full) for r in repos)
    print(f"embedded {total} issues")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    cfg = config.load_models_config(args.models_config)
    con = _open_db(args.db)
    embedder = embed_mod.FastEmbedEmbedder(cfg.embed_model)
    summary = analyze_mod.analyze(
        con,
        cfg,
        repo=args.repo,
        limit=args.limit,
        full=args.full,
        wait=args.wait,
        embedder=embedder,
        batch_client=llm.AnthropicBatchClient(),
        rubric_path=".github/triage/issue-triage-rubric.md",
        labels_path=".github/triage/labels.yaml",
        proposals_dir=args.proposals_dir,
        log=print,
    )
    print(
        f"classified={summary['classified']} rechecked={summary['rechecked']} "
        f"pairs={summary['pairs']} halted_on_budget={summary['halted_on_budget']}"
    )
    return 0


def _cmd_analyze_status(args: argparse.Namespace) -> int:
    con = _open_db(args.db)
    status = analyze_mod.analyze_status(con)
    print(
        f"open batches: {len(status['open_batches'])}; "
        f"today spend: ${status['today_spend_usd']:.4f}"
    )
    for b in status["open_batches"]:
        print(f"  {b['batch_id']} [{b['stage']}] {b['status']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triage-verse")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="mirror issues/PRs/comments to SQLite")
    p_sync.add_argument("--db", default=DEFAULT_DB)
    p_sync.add_argument("--config", default=DEFAULT_CONFIG)
    p_sync.add_argument("--repo", help="sync only this owner/name")
    p_sync.add_argument(
        "--full", action="store_true", help="ignore cursors and re-walk everything"
    )
    p_sync.set_defaults(func=_cmd_sync)

    p_snap = sub.add_parser("snapshot", help="publish or fetch mirror snapshots")
    snap_sub = p_snap.add_subparsers(dest="snapshot_command", required=True)

    p_pub = snap_sub.add_parser("publish")
    p_pub.add_argument("--db", default=DEFAULT_DB)
    p_pub.add_argument(
        "--dated",
        action="store_true",
        help="also cut a dated mirror-YYYY-MM-DD restore point",
    )
    p_pub.set_defaults(func=_cmd_snapshot_publish)

    p_boot = snap_sub.add_parser("bootstrap")
    p_boot.add_argument("--db", default=DEFAULT_DB)
    p_boot.add_argument("--force", action="store_true")
    p_boot.set_defaults(func=_cmd_snapshot_bootstrap)

    p_an = sub.add_parser("analytics", help="compute burndown analytics")
    an_sub = p_an.add_subparsers(dest="analytics_command", required=True)
    p_exp = an_sub.add_parser("export")
    p_exp.add_argument("--db", default=DEFAULT_DB)
    p_exp.add_argument("--out", default=".data/analytics.json")
    p_exp.set_defaults(func=_cmd_analytics_export)

    p_ver = sub.add_parser(
        "verify-counts", help="reconcile mirror vs GitHub open-issue counts"
    )
    p_ver.add_argument("--db", default=DEFAULT_DB)
    p_ver.add_argument("--config", default=DEFAULT_CONFIG)
    p_ver.add_argument(
        "--tolerance",
        type=int,
        default=2,
        help="max mirror-vs-github drift treated as OK",
    )
    p_ver.set_defaults(func=_cmd_verify_counts)

    p_embed = sub.add_parser("embed", help="compute/update issue embeddings")
    p_embed.add_argument("--db", default=DEFAULT_DB)
    p_embed.add_argument("--config", default=DEFAULT_CONFIG)
    p_embed.add_argument("--models-config", default=DEFAULT_MODELS)
    p_embed.add_argument("--repo")
    p_embed.add_argument("--full", action="store_true")
    p_embed.set_defaults(func=_cmd_embed)

    p_an = sub.add_parser("analyze", help="classify + dedup -> proposals (Batch API)")
    p_an.add_argument("--db", default=DEFAULT_DB)
    p_an.add_argument("--models-config", default=DEFAULT_MODELS)
    p_an.add_argument("--repo")
    p_an.add_argument("--limit", type=int)
    p_an.add_argument("--full", action="store_true")
    p_an.add_argument("--wait", action="store_true")
    p_an.add_argument("--proposals-dir", default=DEFAULT_PROPOSALS)
    p_an.set_defaults(func=_cmd_analyze)

    p_st = sub.add_parser(
        "analyze-status", help="show in-flight batches and today's spend"
    )
    p_st.add_argument("--db", default=DEFAULT_DB)
    p_st.set_defaults(func=_cmd_analyze_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
