"""Burndown and flux analytics over the mirror (issues only, not PRs).

All series are computed from created_at/closed_at, so history is correct
retroactively — including for periods before this project existed.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone


def _iso_week(stamp: str) -> str:
    day = date.fromisoformat(stamp[:10])
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _mondays(first_stamp: str, last_stamp: str) -> list[date]:
    start = date.fromisoformat(first_stamp[:10])
    end = date.fromisoformat(last_stamp[:10])
    monday = start - timedelta(days=start.weekday())
    out = []
    while monday <= end:
        out.append(monday)
        monday += timedelta(days=7)
    return out


def _issue_stamps(con: sqlite3.Connection, repo: str | None):
    where = "WHERE is_pr=0" + (" AND repo=:repo" if repo else "")
    rows = con.execute(
        f"SELECT created_at, closed_at FROM issues {where}",
        {"repo": repo} if repo else {},
    ).fetchall()
    created = sorted(r["created_at"] for r in rows)
    closed = sorted(r["closed_at"] for r in rows if r["closed_at"])
    return created, closed


def weekly_open_counts(
    con: sqlite3.Connection, *, repo: str | None = None, as_of: str | None = None
) -> list[dict]:
    """Open-issue count measured at 00:00 UTC at the START of each Monday.

    An issue created later that same Monday first appears in the FOLLOWING
    week's data point, so the first series entry is typically 0. This
    start-of-week sampling is intentional and consistent across the series.
    """
    created, closed = _issue_stamps(con, repo)
    if not created:
        return []
    end = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    series = []
    for monday in _mondays(created[0], end):
        boundary = monday.isoformat() + "T00:00:00Z"
        open_count = bisect_right(created, boundary) - bisect_right(closed, boundary)
        year, week, _ = monday.isocalendar()
        series.append({"week": f"{year}-W{week:02d}", "open": open_count})
    return series


def weekly_flux(con: sqlite3.Connection, *, repo: str | None = None) -> list[dict]:
    created, closed = _issue_stamps(con, repo)
    counts: dict[str, dict] = {}
    for stamp in created:
        week = _iso_week(stamp)
        counts.setdefault(week, {"week": week, "opened": 0, "closed": 0})
        counts[week]["opened"] += 1
    for stamp in closed:
        week = _iso_week(stamp)
        counts.setdefault(week, {"week": week, "opened": 0, "closed": 0})
        counts[week]["closed"] += 1
    return sorted(counts.values(), key=lambda f: f["week"])


def close_reason_mix(
    con: sqlite3.Connection, *, repo: str | None = None
) -> dict[str, int]:
    where = "WHERE is_pr=0 AND state='CLOSED'" + (" AND repo=:repo" if repo else "")
    rows = con.execute(
        f"SELECT COALESCE(state_reason, 'UNSPECIFIED') AS reason,"
        f" COUNT(*) AS n FROM issues {where} GROUP BY reason",
        {"repo": repo} if repo else {},
    ).fetchall()
    return {r["reason"]: r["n"] for r in rows}


def export(con: sqlite3.Connection, out_path: str | pathlib.Path) -> dict:
    repos = [
        r["repo"]
        for r in con.execute(
            "SELECT DISTINCT repo FROM issues WHERE is_pr=0 ORDER BY repo"
        )
    ]
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "weekly_open": weekly_open_counts(con),
            "weekly_flux": weekly_flux(con),
            "close_reasons": close_reason_mix(con),
        },
        "repos": {
            repo: {
                "weekly_open": weekly_open_counts(con, repo=repo),
                "weekly_flux": weekly_flux(con, repo=repo),
                "close_reasons": close_reason_mix(con, repo=repo),
            }
            for repo in repos
        },
    }
    out_path = pathlib.Path(out_path)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    return payload
