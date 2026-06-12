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
from .gh import gh_graphql

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
