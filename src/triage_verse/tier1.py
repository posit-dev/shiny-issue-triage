"""Tier 1 "already fixed?" checks: candidate selection + read-only session."""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone

from . import db as db_mod
from . import prompts, proposals, review_queue, spend


def select_candidates(con, repos, *, proposals_dir, limit: int) -> list[dict]:
    seen_tier1 = {
        (r.get("repo"), r.get("issue"))
        for r in review_queue.iter_jsonl_records(proposals_dir)
        if r.get("origin") == "tier1"
    }
    placeholders = ",".join("?" for _ in repos)
    rows = con.execute(
        f"""
        SELECT i.repo AS repo, i.number AS number, i.updated_at AS updated_at
        FROM issues i
        WHERE i.state = 'OPEN' AND i.repo IN ({placeholders})
          AND (
            EXISTS (
              SELECT 1 FROM classifications c
              WHERE c.repo = i.repo AND c.number = i.number
                AND c.close_candidate_json IS NOT NULL
                AND json_extract(c.close_candidate_json, '$.reason') = 'fixed'
            )
            OR EXISTS (
              SELECT 1 FROM prs p
              WHERE p.repo = i.repo AND p.merged = 1
                AND EXISTS (
                  SELECT 1 FROM json_each(p.closing_issue_refs_json) je
                  WHERE je.value = i.number
                )
            )
          )
        ORDER BY i.updated_at ASC, i.number ASC
        """,
        list(repos),
    ).fetchall()
    out = []
    for row in rows:
        key = (row["repo"], row["number"])
        if key in seen_tier1:
            continue
        out.append({"repo": row["repo"], "issue": row["number"]})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Session runner, prompt construction, proposal emission
# ---------------------------------------------------------------------------

_VERDICTS = {"fixed", "not-fixed", "unclear"}
_CLI_TIMEOUT = 600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_prompt(issue: dict, comments: list[dict]) -> str:
    thread = "\n\n".join(f"@{c['author']}: {c['body']}" for c in comments)
    return (
        "You are checking whether a reported issue has ALREADY been fixed in this "
        "repository. Search NEWS/NEWS.md/changelog, `git log`, and merged history. "
        "Do not modify any files.\n\n"
        + prompts.delimit("ISSUE_TITLE", issue.get("title"))
        + "\n"
        + prompts.delimit("ISSUE_BODY", issue.get("body"))
        + "\n"
        + prompts.delimit("COMMENTS", thread)
        + "\n\nRespond with ONLY a JSON object: "
        '{"verdict": "fixed|not-fixed|unclear", "fixed_in": string|null, '
        '"evidence": [urls or commit shas], "summary": string, "confidence": number}.'
    )


def parse_session(text: str) -> dict:
    t = text.strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        raise ValueError("no JSON object in tier1 output")
    import json

    data = json.loads(t[i : j + 1])
    if data.get("verdict") not in _VERDICTS:
        raise ValueError(f"invalid tier1 verdict: {data.get('verdict')!r}")
    return data


def _default_checkout(repo: str, cache_dir: str) -> str:
    import pathlib

    dest = pathlib.Path(cache_dir) / repo.replace("/", "__")
    if (dest / ".git").exists():
        subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1"], check=False)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["gh", "repo", "clone", repo, str(dest), "--", "--depth", "1"], check=True
        )
    return str(dest)


def _default_runner(repo_dir: str, prompt: str) -> tuple[str, float]:
    proc = subprocess.run(
        [
            "claude",
            "-p",
            prompt,
            "--add-dir",
            repo_dir,
            "--allowedTools",
            "Read,Grep,Glob,Bash(git log:*),Bash(git show:*)",
        ],
        capture_output=True,
        text=True,
        cwd=repo_dir,
        timeout=_CLI_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout, 0.0  # cost parsed from stream-json in a later pass; 0 for now


def _today_tier1_count(records, today: str) -> int:
    return sum(
        1
        for r in records
        if r.get("origin") == "tier1" and (r.get("created_at", "")[:10] == today)
    )


def run(
    con,
    repos,
    *,
    cfg,
    proposals_dir,
    run_gh,
    runner=_default_runner,
    checkout=_default_checkout,
    cache_dir=".data/checkouts",
    today=None,
    log=print,
) -> dict:
    today = today or _now()[:10]
    cap = cfg.tiers.tier1_max_per_day
    existing = review_queue.iter_jsonl_records(proposals_dir)
    remaining = max(0, cap - _today_tier1_count(existing, today))
    cands = select_candidates(con, repos, proposals_dir=proposals_dir, limit=remaining)
    run_id = db_mod.start_run(con, "tier1")
    sessions = proposals_made = 0
    halted = False
    for c in cands:
        if spend.breaker_tripped(con, cfg):
            halted = True
            break
        issue = db_mod.get_issue(con, c["repo"], c["issue"])
        if issue is None:
            continue
        comments = [dict(r) for r in db_mod.get_comments(con, c["repo"], c["issue"])]
        repo_dir = checkout(c["repo"], cache_dir)
        text, cost = runner(repo_dir, build_prompt(dict(issue), comments))
        sessions += 1
        if cost:
            db_mod.insert_spend(con, run_id, "tier1", cfg.classify.model, 0, 0, 0, cost)
        try:
            verdict = parse_session(text)
        except ValueError as exc:
            log(f"tier1 {c['repo']}#{c['issue']}: unparseable ({exc})")
            _emit_noop(c, "unclear", proposals_dir)
            continue
        if verdict["verdict"] == "fixed":
            _emit_close(c, issue, verdict, proposals_dir, run_id)
            proposals_made += 1
        else:
            _emit_noop(c, verdict["verdict"], proposals_dir)
    return {
        "sessions": sessions,
        "proposals": proposals_made,
        "halted_on_budget": halted,
    }


def _emit_close(c, issue, verdict, proposals_dir, run_id) -> None:
    evidence = [
        f"https://github.com/{c['repo']}/issues/{c['issue']}",
        *verdict.get("evidence", []),
    ]
    rec = {
        "id": uuid.uuid4().hex,
        "repo": c["repo"],
        "issue": c["issue"],
        "issue_updated_at": issue["updated_at"],
        "run_id": run_id,
        "model": "tier1",
        "origin": "tier1",
        "confidence": verdict.get("confidence", 0.0),
        "evidence": evidence,
        "action": "close",
        "params": {"reason": "fixed"},
        "rationale": verdict.get("summary", ""),
        "created_at": _now(),
    }
    proposals.write([rec], proposals_dir)


def _emit_noop(c, verdict, proposals_dir) -> None:
    rec = {
        "id": uuid.uuid4().hex,
        "repo": c["repo"],
        "issue": c["issue"],
        "origin": "tier1",
        "action": "no-op",
        "verdict": verdict,
        "created_at": _now(),
    }
    proposals.write([rec], proposals_dir)
