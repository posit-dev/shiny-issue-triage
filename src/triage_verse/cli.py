"""triage-verse command-line interface."""

from __future__ import annotations

import argparse
import io
import os
import pathlib
import sys

from . import analytics as analytics_mod
from . import analyze as analyze_mod
from . import config, db
from . import embed as embed_mod
from . import executor as executor_mod
from . import gh
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


def _env_default(value: str | None, env: str, fallback: str) -> str:
    return value if value is not None else os.environ.get(env, fallback)


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


def _run_analyze(args: argparse.Namespace) -> None:
    """Shared analyze logic used by both the `analyze` command and `steady-state`."""
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
        batch_client=llm.make_batch_client(cfg, log=print),
        rubric_path=".github/triage/issue-triage-rubric.md",
        labels_path=".github/triage/labels.yaml",
        proposals_dir=args.proposals_dir,
        log=print,
    )
    print(
        f"classified={summary['classified']} rechecked={summary['rechecked']} "
        f"pairs={summary['pairs']} halted_on_budget={summary['halted_on_budget']}"
    )


def _cmd_analyze(args: argparse.Namespace) -> int:
    _run_analyze(args)
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


def _cmd_execute(args: argparse.Namespace) -> int:
    args.db = _env_default(args.db, "TRIAGE_VERSE_DB", DEFAULT_DB)
    args.decisions_dir = _env_default(
        args.decisions_dir, "TRIAGE_VERSE_DECISIONS", ".data/decisions"
    )
    args.proposals_dir = _env_default(
        args.proposals_dir, "TRIAGE_VERSE_PROPOSALS", DEFAULT_PROPOSALS
    )
    args.results_dir = _env_default(
        args.results_dir, "TRIAGE_VERSE_RESULTS", ".data/results"
    )
    con = _open_db(args.db)
    summary = executor_mod.execute(
        con,
        decisions_dir=args.decisions_dir,
        proposals_dir=args.proposals_dir,
        results_dir=args.results_dir,
        labels_path=args.labels,
        templates_dir=args.templates,
        run_gh=gh.run_gh,
        apply=args.apply,
        repo=args.repo,
        limit=args.limit,
    )
    print(f"batch {summary['batch_id']}: {summary['counts']}")
    return 1 if summary["counts"]["error"] else 0


def _run_git(args, *, cwd=None):
    import subprocess
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", check=True,
    ).stdout


def _ensure_state_clone(work_dir: str, branch: str) -> None:
    if pathlib.Path(work_dir, ".git").exists():
        return
    origin = gh.run_gh(["repo", "view", "--json", "url", "-q", ".url"]).strip()
    _run_git(["clone", "--branch", branch, "--single-branch", origin, work_dir])


def _state_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cmd_state_pull(args: argparse.Namespace) -> int:
    from . import state
    work = os.environ.get("TRIAGE_VERSE_STATE_WORKDIR", ".data/triage-state")
    _ensure_state_clone(work, args.branch)
    res = state.pull(data_dir=args.data_dir, work_dir=work, run_git=_run_git, branch=args.branch)
    print(f"pulled: {res['files_updated']} files updated")
    return 0


def _cmd_state_push(args: argparse.Namespace) -> int:
    from . import state
    con = _open_db(args.db)
    repos = [r.full for r in config.load_repos(args.config)]
    work = os.environ.get("TRIAGE_VERSE_STATE_WORKDIR", ".data/triage-state")
    _ensure_state_clone(work, args.branch)
    res = state.push(
        con, repos, data_dir=args.data_dir, work_dir=work, run_git=_run_git,
        branch=args.branch, now=_state_now(),
    )
    print(f"push: {'committed' if res['pushed'] else 'no changes'} ({res['records']} records)")
    return 0


def _cmd_undo(args: argparse.Namespace) -> int:
    args.db = _env_default(args.db, "TRIAGE_VERSE_DB", DEFAULT_DB)
    args.results_dir = _env_default(
        args.results_dir, "TRIAGE_VERSE_RESULTS", ".data/results"
    )
    con = _open_db(args.db)
    summary = executor_mod.undo(
        con,
        results_dir=args.results_dir,
        batch_id=args.batch,
        issue=args.issue,
        run_gh=gh.run_gh,
        apply=args.apply,
    )
    print(f"batch {summary['batch_id']}: {summary['counts']}")
    return 1 if summary["counts"]["error"] else 0


def _cmd_steady_state(args: argparse.Namespace) -> int:
    from . import state, steady_state

    con = _open_db(args.db)
    repos = [r.full for r in config.load_repos(args.config)]
    work = os.environ.get("TRIAGE_VERSE_STATE_WORKDIR", ".data/triage-state")

    def _pull():
        _ensure_state_clone(work, args.branch)
        state.pull(data_dir=args.data_dir, work_dir=work, run_git=_run_git, branch=args.branch)

    def _sync():
        sync_mod.sync_all(con, repos, full=False, log=print)

    def _analyze():
        _run_analyze(args)

    def _tier1():
        if not args.no_tier1:
            from . import tier1
            tier1.run(con, repos, cfg=config.load_models_config(args.models_config),
                      proposals_dir=args.proposals_dir, run_gh=gh.run_gh, log=print)

    def _push():
        state.push(con, repos, data_dir=args.data_dir, work_dir=work, run_git=_run_git,
                   branch=args.branch, now=_state_now())

    def _snapshot():
        snapshot_mod.publish(args.db, dated=False)

    stages = [("state-pull", _pull), ("sync", _sync), ("embed-analyze", _analyze),
              ("tier1", _tier1), ("state-push", _push), ("snapshot", _snapshot)]
    if args.dry_run:
        for name, _ in stages:
            print(f"would run: {name}")
        return 0
    res = steady_state.run(stages)
    return 1 if res["failed"] else 0


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

    p_exec = sub.add_parser(
        "execute", help="apply approved decisions (dry-run by default)"
    )
    p_exec.add_argument("--db", default=None)
    p_exec.add_argument("--decisions-dir", default=None)
    p_exec.add_argument("--proposals-dir", default=None)
    p_exec.add_argument("--results-dir", default=None)
    p_exec.add_argument("--labels", default=".github/triage/labels.yaml")
    p_exec.add_argument("--templates", default="config/templates")
    p_exec.add_argument("--repo", help="only decisions for this owner/name")
    p_exec.add_argument("--limit", type=int, help="max decisions this run")
    p_exec.add_argument(
        "--apply", action="store_true", help="perform mutations (default: dry-run)"
    )
    p_exec.set_defaults(func=_cmd_execute)

    p_undo = sub.add_parser(
        "undo", help="reverse an executed batch (dry-run by default)"
    )
    p_undo.add_argument("--db", default=None)
    p_undo.add_argument("--results-dir", default=None)
    p_undo.add_argument("--batch", required=True, help="batch id to reverse")
    p_undo.add_argument("--issue", help="restrict to one issue, e.g. owner/name#7")
    p_undo.add_argument(
        "--apply", action="store_true", help="perform mutations (default: dry-run)"
    )
    p_undo.set_defaults(func=_cmd_undo)

    p_state = sub.add_parser("state", help="sync state bus via git")
    state_sub = p_state.add_subparsers(dest="state_command", required=True)

    p_pull = state_sub.add_parser("pull", help="pull state from remote branch")
    p_pull.add_argument("--branch", default="triage-state")
    p_pull.add_argument("--data-dir", default=".data")
    p_pull.set_defaults(func=_cmd_state_pull)

    p_push = state_sub.add_parser("push", help="push state to remote branch")
    p_push.add_argument("--branch", default="triage-state")
    p_push.add_argument("--data-dir", default=".data")
    p_push.add_argument("--db", default=DEFAULT_DB)
    p_push.add_argument("--config", default=DEFAULT_CONFIG)
    p_push.set_defaults(func=_cmd_state_push)

    p_ss = sub.add_parser("steady-state", help="run full steady-state loop")
    p_ss.add_argument("--db", default=os.environ.get("TRIAGE_VERSE_DB", DEFAULT_DB))
    p_ss.add_argument("--config", default=DEFAULT_CONFIG)
    p_ss.add_argument("--models-config", default=DEFAULT_MODELS)
    p_ss.add_argument("--proposals-dir", default=DEFAULT_PROPOSALS)
    p_ss.add_argument("--data-dir", default=".data")
    p_ss.add_argument("--branch", default="triage-state")
    p_ss.add_argument("--no-tier1", action="store_true", help="skip tier-1 stage")
    p_ss.add_argument("--dry-run", action="store_true", help="list stages without running")
    p_ss.set_defaults(func=_cmd_steady_state, repo=None, limit=None, full=False, wait=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Force line buffering even when stdout is redirected to a file (a
    # background process, a scheduled job, `> log.txt`, ...), where Python
    # otherwise defaults to block buffering. Without this, log() output can
    # sit unflushed for the entire run instead of being tailable live.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    return args.func(args)
