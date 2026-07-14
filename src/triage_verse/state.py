"""Sync append-only JSONL state + cursors with the triage-state branch."""

from __future__ import annotations

import json
import pathlib
from typing import Callable

from . import db as db_mod

STATE_FILES = ("proposals", "decisions", "results")
CURSORS_FILE = "cursors.json"

RunGit = Callable[..., str]


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def union_merge_lines(existing: str, incoming: str) -> str:
    """Existing lines plus incoming lines not already present, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for ln in _lines(existing) + _lines(incoming):
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return "".join(ln + "\n" for ln in out)


def export_cursors(con, repos: list[str], *, now: str) -> dict:
    out: dict[str, dict] = {}
    for repo in repos:
        out[repo] = {
            "issues": db_mod.get_cursor(con, repo, "issues"),
            "prs": db_mod.get_cursor(con, repo, "prs"),
            "comments": db_mod.get_cursor(con, repo, "comments"),
        }
    return {"exported_at": now, "repos": out}


def _jsonl_paths(base: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for sub in STATE_FILES for p in (base / sub).glob("**/*.jsonl"))


def _rel(base: pathlib.Path, path: pathlib.Path) -> str:
    return str(path.relative_to(base))


def _fetch_branch(run_git: RunGit, work_dir: pathlib.Path, branch: str) -> bool:
    """Fetch branch from origin; return True if it exists on remote."""
    import subprocess

    try:
        run_git(["fetch", "origin", branch], cwd=str(work_dir))
        return True
    except subprocess.CalledProcessError:
        return False


def _checkout_or_create(run_git: RunGit, work_dir: pathlib.Path, branch: str) -> None:
    """Checkout branch, creating an orphan if it doesn't exist locally."""
    import subprocess

    try:
        run_git(["checkout", branch], cwd=str(work_dir))
    except subprocess.CalledProcessError:
        run_git(["checkout", "--orphan", branch], cwd=str(work_dir))
        # Remove any files from the index in the new orphan branch
        try:
            run_git(["rm", "-rf", "."], cwd=str(work_dir))
        except subprocess.CalledProcessError:
            pass  # nothing to remove is fine


def pull(*, data_dir, work_dir, run_git: RunGit, branch: str = "triage-state") -> dict:
    data_dir = pathlib.Path(data_dir)
    work_dir = pathlib.Path(work_dir)
    _fetch_branch(run_git, work_dir, branch)
    _checkout_or_create(run_git, work_dir, branch)
    updated = 0
    for src in _jsonl_paths(work_dir):
        rel = _rel(work_dir, src)
        dst = data_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        existing = dst.read_text(encoding="utf-8") if dst.exists() else ""
        merged = union_merge_lines(existing, src.read_text(encoding="utf-8"))
        if merged != existing:
            dst.write_text(merged, encoding="utf-8")
            updated += 1
    cur = work_dir / CURSORS_FILE
    if cur.exists():
        (data_dir / CURSORS_FILE).write_text(
            cur.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return {"files_updated": updated}


def push(
    con,
    repos,
    *,
    data_dir,
    work_dir,
    run_git: RunGit,
    branch: str = "triage-state",
    now: str,
    log: Callable[[str], None] = print,
) -> dict:
    data_dir = pathlib.Path(data_dir)
    work_dir = pathlib.Path(work_dir)
    # Pull first so we never clobber the remote.
    _fetch_branch(run_git, work_dir, branch)
    _checkout_or_create(run_git, work_dir, branch)
    records = 0
    for src in _jsonl_paths(data_dir):
        rel = _rel(data_dir, src)
        dst = work_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        existing = dst.read_text(encoding="utf-8") if dst.exists() else ""
        merged = union_merge_lines(existing, src.read_text(encoding="utf-8"))
        if merged != existing:
            dst.write_text(merged, encoding="utf-8")
        records += len(_lines(src.read_text(encoding="utf-8")))
    (work_dir / CURSORS_FILE).write_text(
        json.dumps(export_cursors(con, list(repos), now=now), indent=2) + "\n",
        encoding="utf-8",
    )
    run_git(["add", "-A"], cwd=str(work_dir))
    status = run_git(["status", "--porcelain"], cwd=str(work_dir))
    if not status.strip():
        return {"pushed": False, "records": records}
    run_git(["commit", "-m", f"state: sync {records} records"], cwd=str(work_dir))
    run_git(["push", "origin", branch], cwd=str(work_dir))
    return {"pushed": True, "records": records}
