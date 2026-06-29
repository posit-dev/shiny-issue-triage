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
        "labels_json": json.dumps(
            [label["name"] for label in node["labels"]["nodes"]]),
        "assignees_json": json.dumps(
            [a["login"] for a in node["assignees"]["nodes"]]),
        "milestone": milestone.get("title"),
        "comment_count": node["comments"]["totalCount"],
        "reaction_count": node["reactions"]["totalCount"],
        "is_pr": 0,
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node.get("closedAt"),
    }


def _walk_updated_desc(con: sqlite3.Connection, repo: str, kind: str,
                       query: str, connection_key: str,
                       upsert: Callable[[sqlite3.Connection, dict], int],
                       graphql: Callable, full: bool) -> int:
    owner, name = repo.split("/")
    cursor = None if full else db.get_cursor(con, repo, kind)
    after = None
    newest = cursor
    count = 0
    while True:
        data = graphql(query, {"owner": owner, "name": name, "after": after})
        conn = data["repository"][connection_key]
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
    return count


def sync_issues(con: sqlite3.Connection, repo: str, *,
                graphql: Callable = gh_graphql, full: bool = False) -> int:
    def upsert(con_: sqlite3.Connection, node: dict) -> int:
        db.upsert_issue(con_, parse_issue_node(repo, node))
        return 1

    return _walk_updated_desc(con, repo, "issues", ISSUES_QUERY, "issues",
                              upsert, graphql, full)


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
        "labels_json": json.dumps(
            [label["name"] for label in node["labels"]["nodes"]]),
        "assignees_json": json.dumps(
            [a["login"] for a in node["assignees"]["nodes"]]),
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
            [n["number"] for n in node["closingIssuesReferences"]["nodes"]]),
        "head_ref": node.get("headRefName"),
        "base_ref": node.get("baseRefName"),
    }
    return issue_row, pr_row


def sync_prs(con: sqlite3.Connection, repo: str, *,
             graphql: Callable = gh_graphql, full: bool = False) -> int:
    def upsert(con_: sqlite3.Connection, node: dict) -> int:
        issue_row, pr_row = parse_pr_node(repo, node)
        db.upsert_issue(con_, issue_row)
        db.upsert_pr(con_, pr_row)
        return 1

    return _walk_updated_desc(con, repo, "prs", PRS_QUERY, "pullRequests",
                              upsert, graphql, full)


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


def sync_comments(con: sqlite3.Connection, repo: str, *,
                  api: Callable = None, full: bool = False) -> int:
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
        path = (f"repos/{repo}/issues/comments"
                f"?sort=updated&direction=asc&per_page=100"
                f"&since={since}&page={page}")
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


def sync_all(con: sqlite3.Connection, repos: list[str], *,
             full: bool = False, log: Callable[[str], None] = print) -> dict:
    run_id = db.start_run(con, "sync")
    totals = {"repos": 0, "issues": 0, "prs": 0, "comments": 0}
    try:
        for repo in repos:
            log(f"syncing {repo} ...")
            totals["issues"] += sync_issues(con, repo, full=full)
            totals["prs"] += sync_prs(con, repo, full=full)
            totals["comments"] += sync_comments(con, repo, full=full)
            totals["repos"] += 1
            log(f"  done {repo}")
    finally:
        db.finish_run(con, run_id, totals)
    return totals
