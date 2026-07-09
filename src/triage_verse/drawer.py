"""Assemble a full issue/PR view from the mirror for the review-app drawer."""

from __future__ import annotations

import json
import sqlite3

from . import db


def load_item(con: sqlite3.Connection, repo: str, number: int) -> dict | None:
    """Full drawer payload for one item, or None if it isn't in the mirror."""
    issue = db.get_issue(con, repo, number)
    if issue is None:
        return None
    item = dict(issue)
    item["labels"] = json.loads(item.pop("labels_json"))
    item["assignees"] = json.loads(item.pop("assignees_json"))
    item["comments"] = [
        {"author": c["author"], "body": c["body"], "created_at": c["created_at"]}
        for c in db.get_comments(con, repo, number)
    ]
    kind = "pull" if item["is_pr"] else "issues"
    item["github_url"] = f"https://github.com/{repo}/{kind}/{number}"
    item["pr"] = None
    if item["is_pr"]:
        pr = db.get_pr(con, repo, number)
        if pr is not None:
            item["pr"] = {
                "merged": bool(pr["merged"]),
                "merged_at": pr["merged_at"],
                "head_ref": pr["head_ref"],
                "base_ref": pr["base_ref"],
            }
    return item
