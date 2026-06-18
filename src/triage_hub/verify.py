"""Reconcile mirror open-issue counts against GitHub search totals."""

from __future__ import annotations

import sqlite3
from typing import Callable

from .gh import gh_json


def verify_counts(con: sqlite3.Connection, repos: list[str], *,
                  api: Callable = gh_json, tolerance: int = 2) -> list[dict]:
    results = []
    for repo in repos:
        mirror = con.execute(
            "SELECT COUNT(*) FROM issues"
            " WHERE repo=? AND state='OPEN' AND is_pr=0", (repo,)).fetchone()[0]
        data = api(["api", f"search/issues?q=repo:{repo}+type:issue+state:open"
                    f"&per_page=1"])
        github = data["total_count"]
        results.append({
            "repo": repo,
            "mirror": mirror,
            "github": github,
            "ok": abs(mirror - github) <= tolerance,
        })
    return results
