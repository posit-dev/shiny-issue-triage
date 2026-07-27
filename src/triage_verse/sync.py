"""Incremental GitHub → SQLite sync.

Issues and PRs walk GraphQL connections ordered by updatedAt DESC and stop at
the stored cursor (a timestamp). GitHub bumps an issue's updatedAt on every new
comment, so commenting on an old issue re-enters it into the sync window.
Upserts are idempotent; re-processing rows at the cursor boundary is harmless.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from . import db
from .gh import gh_graphql, gh_json

ISSUES_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 50, orderBy: {field: UPDATED_AT, direction: DESC}, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state stateReason
        author { login }
        labels(first: 50) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        milestone { title }
        comments { totalCount }
        reactions { totalCount }
        createdAt updatedAt closedAt
      }
    }
  }
}
"""


def parse_issue_node(repo: str, node: dict) -> dict:
    author = node.get("author") or {}
    milestone = node.get("milestone") or {}
    return {
        "repo": repo,
        "number": node["number"],
        "title": node["title"],
        "body": node.get("body"),
        "state": node["state"],
        "state_reason": node.get("stateReason"),
        "author": author.get("login"),
        "labels_json": json.dumps([label["name"] for label in node["labels"]["nodes"]]),
        "assignees_json": json.dumps([a["login"] for a in node["assignees"]["nodes"]]),
        "milestone": milestone.get("title"),
        "comment_count": node["comments"]["totalCount"],
        "reaction_count": node["reactions"]["totalCount"],
        "is_pr": 0,
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node.get("closedAt"),
    }


def _walk_updated_desc(
    con: sqlite3.Connection,
    repo: str,
    kind: str,
    query: str,
    connection_key: str,
    upsert: Callable[[sqlite3.Connection, dict], int],
    graphql: Callable,
    full: bool,
) -> tuple[int, int | None]:
    """Walk one UPDATED_AT DESC connection; returns (upserted, reported totalCount).

    The total is whatever the connection reported on its first page, or `None`
    when the query does not ask for `totalCount`. Callers use it to tell an
    exhaustive walk from one that pagination raced (see `reconcile_repo`).
    """
    owner, name = repo.split("/")
    cursor = None if full else db.get_cursor(con, repo, kind)
    after = None
    newest = cursor
    count = 0
    total: int | None = None
    while True:
        data = graphql(query, {"owner": owner, "name": name, "after": after})
        conn = data["repository"][connection_key]
        if total is None:
            total = conn.get("totalCount")
        stop = False
        for node in conn["nodes"]:
            if cursor is not None and node["updatedAt"] < cursor:
                stop = True
                break
            count += upsert(con, node)
            if newest is None or node["updatedAt"] > newest:
                newest = node["updatedAt"]
        if stop or not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    if newest is not None:
        db.set_cursor(con, repo, kind, newest)
    con.commit()
    return count, total


def reconcile_repo(
    con: sqlite3.Connection,
    repo: str,
    seen: set[int],
    *,
    total_count: int | None = None,
    log: Callable[[str], None] = print,
) -> list[int]:
    """Delete mirrored issues of `repo` that GitHub no longer lists.

    Only sound after an exhaustive walk (`full=True`): an incremental sync stops
    at the stored cursor and legitimately never sees older issues, so absence
    there means nothing. A transferred issue leaves its source repo altogether,
    so absence from a full walk is the only signal available.

    Refuses to act on two kinds of incomplete walk:

    * **Short walk.** `total_count` is the issue count GitHub itself reported for
      the connection. The walk pages an `UPDATED_AT DESC` ordering, so an issue
      that gets a comment mid-walk while sitting on a not-yet-visited page jumps
      ahead of the cursor and is never returned. Upserts do not care, but
      deletion very much does, so seeing fewer issues than GitHub reported means
      no deletion at all this run. In a healthy repo there are normally zero
      deletion candidates anyway, so the guard costs nothing.
    * **Empty response.** The walk saw no issues at all but the mirror holds
      some: an exception-free empty response is far more likely to mean Issues
      are disabled, a permissions problem, or an eventual-consistency blip than a
      repo that genuinely lost every issue. A truly empty repo has no mirrored
      rows either, so the guard costs nothing in the legitimate case.
    """
    rows = con.execute(
        "SELECT number FROM issues WHERE repo=? AND is_pr=0", (repo,)
    ).fetchall()
    if total_count is not None and len(seen) < total_count:
        log(
            f"  reconcile {repo}: REFUSING to retire anything -- the walk saw "
            f"{len(seen)} issue(s) but GitHub reports {total_count}, so pagination "
            f"raced an update and an unseen issue may still be live. Re-run "
            f"`sync --full` to retire genuinely absent issues."
        )
        return []
    if not seen and rows:
        log(
            f"  reconcile {repo}: REFUSING to retire {len(rows)} mirrored issue(s) "
            f"-- GitHub returned no issues at all, which usually means an API or "
            f"permissions problem rather than a genuinely empty repo. Re-run "
            f"`sync --full` once the cause is resolved."
        )
        return []
    gone = sorted(r["number"] for r in rows if r["number"] not in seen)
    for number in gone:
        db.delete_issue(con, repo, number)
    con.commit()
    # A destructive stage announces itself even when it deletes nothing, so an
    # operator reading the log can see the step ran at all.
    if gone:
        log(
            f"  reconcile {repo}: retired {len(gone)} issue(s) GitHub no longer "
            f"lists (transferred or deleted): {gone}"
        )
    else:
        log(f"  reconcile {repo}: nothing to retire")
    return gone


def sync_issues(
    con: sqlite3.Connection,
    repo: str,
    *,
    graphql: Callable = gh_graphql,
    full: bool = False,
    log: Callable[[str], None] = print,
    on_retire: Callable[[list[int]], None] | None = None,
) -> int:
    """Sync `repo`'s issues; on a full walk, retire rows GitHub no longer lists.

    `on_retire` receives the retired issue numbers, so a caller (e.g. `sync_all`)
    can report the destructive step in its machine-readable totals rather than
    only in the log.
    """
    seen: set[int] = set()

    def upsert(con_: sqlite3.Connection, node: dict) -> int:
        db.upsert_issue(con_, parse_issue_node(repo, node))
        seen.add(node["number"])
        return 1

    count, total = _walk_updated_desc(
        con, repo, "issues", ISSUES_QUERY, "issues", upsert, graphql, full
    )
    # A full walk is exhaustive (no cursor, so it exits only when the connection
    # is drained), and an exception would have propagated before reaching here.
    if full:
        gone = reconcile_repo(con, repo, seen, total_count=total, log=log)
        if on_retire is not None:
            on_retire(gone)
    return count


PRS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, orderBy: {field: UPDATED_AT, direction: DESC}, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state
        author { login }
        labels(first: 50) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        milestone { title }
        comments { totalCount }
        createdAt updatedAt closedAt
        merged mergedAt headRefName baseRefName
        closingIssuesReferences(first: 10) { nodes { number } }
      }
    }
  }
}
"""


def parse_pr_node(repo: str, node: dict) -> tuple[dict, dict]:
    author = node.get("author") or {}
    milestone = node.get("milestone") or {}
    issue_row = {
        "repo": repo,
        "number": node["number"],
        "title": node["title"],
        "body": node.get("body"),
        "state": node["state"],
        "state_reason": None,
        "author": author.get("login"),
        "labels_json": json.dumps([label["name"] for label in node["labels"]["nodes"]]),
        "assignees_json": json.dumps([a["login"] for a in node["assignees"]["nodes"]]),
        "milestone": milestone.get("title"),
        "comment_count": node["comments"]["totalCount"],
        "reaction_count": 0,
        "is_pr": 1,
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node.get("closedAt"),
    }
    pr_row = {
        "repo": repo,
        "number": node["number"],
        "merged": 1 if node.get("merged") else 0,
        "merged_at": node.get("mergedAt"),
        "closing_issue_refs_json": json.dumps(
            [n["number"] for n in node["closingIssuesReferences"]["nodes"]]
        ),
        "head_ref": node.get("headRefName"),
        "base_ref": node.get("baseRefName"),
    }
    return issue_row, pr_row


def sync_prs(
    con: sqlite3.Connection,
    repo: str,
    *,
    graphql: Callable = gh_graphql,
    full: bool = False,
) -> int:
    def upsert(con_: sqlite3.Connection, node: dict) -> int:
        issue_row, pr_row = parse_pr_node(repo, node)
        db.upsert_issue(con_, issue_row)
        db.upsert_pr(con_, pr_row)
        return 1

    # PRs are never reconciled, so the reported total is unused here.
    count, _total = _walk_updated_desc(
        con, repo, "prs", PRS_QUERY, "pullRequests", upsert, graphql, full
    )
    return count


def parse_comment(repo: str, item: dict) -> dict:
    user = item.get("user") or {}
    issue_number = int(item["issue_url"].rstrip("/").rsplit("/", 1)[1])
    return {
        "repo": repo,
        "issue_number": issue_number,
        "comment_id": item["id"],
        "author": user.get("login"),
        "body": item.get("body"),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def sync_comments(
    con: sqlite3.Connection,
    repo: str,
    *,
    api: Callable | None = None,
    full: bool = False,
) -> int:
    """Repo-wide issue-comment listing (covers issue and PR discussion
    threads; PR diff-review comments are out of scope for the mirror)."""
    if api is None:
        api = gh_json
    cursor = None if full else db.get_cursor(con, repo, "comments")
    since = cursor or "1970-01-01T00:00:00Z"
    newest = cursor
    count = 0
    page = 1
    while True:
        path = (
            f"repos/{repo}/issues/comments"
            f"?sort=updated&direction=asc&per_page=100"
            f"&since={since}&page={page}"
        )
        items = api(["api", path]) or []
        for item in items:
            row = parse_comment(repo, item)
            db.upsert_comment(con, row)
            count += 1
            if newest is None or row["updated_at"] > newest:
                newest = row["updated_at"]
        if len(items) < 100:
            break
        page += 1
    if newest is not None:
        db.set_cursor(con, repo, "comments", newest)
    con.commit()
    return count


def sync_all(
    con: sqlite3.Connection,
    repos: list[str],
    *,
    full: bool = False,
    log: Callable[[str], None] = print,
) -> dict:
    run_id = db.start_run(con, "sync")
    # "retired" counts mirror rows deleted because GitHub no longer lists the
    # issue: a destructive step, so it is reported in the machine-readable totals
    # (the --json envelope and the runs table) and not only in the log.
    totals = {"repos": 0, "issues": 0, "prs": 0, "comments": 0, "retired": 0}

    def note_retired(gone: list[int]) -> None:
        totals["retired"] += len(gone)

    try:
        for repo in repos:
            log(f"syncing {repo} ...")
            totals["issues"] += sync_issues(
                con, repo, full=full, log=log, on_retire=note_retired
            )
            totals["prs"] += sync_prs(con, repo, full=full)
            totals["comments"] += sync_comments(con, repo, full=full)
            totals["repos"] += 1
            log(f"  done {repo}")
    finally:
        db.finish_run(con, run_id, totals)
    return totals
