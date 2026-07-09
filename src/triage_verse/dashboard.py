"""Dashboard-tab metrics and inline-SVG charts for the review app.

Pure module (no Shiny import): everything here is computed from the mirror
connection and the proposals/decisions JSONL trees, and returns plain data
or SVG strings, so it stays unit-testable without a browser.
"""

from __future__ import annotations

import html
import pathlib
import sqlite3
from collections import Counter

from .analytics import _iso_week
from .review_queue import iter_jsonl_records

# Validated categorical slots (blue, aqua). Aqua is below 3:1 contrast on a
# light surface, so every chart ships with a legend and an adjacent table.
SERIES_COLORS = ("#2a78d6", "#1baf7a")


def triage_coverage(con: sqlite3.Connection) -> list[dict]:
    """Per repo: open issues and how many of them have a classification."""
    rows = con.execute(
        "SELECT i.repo AS repo, COUNT(*) AS open,"
        " SUM(c.number IS NOT NULL) AS classified"
        " FROM issues i LEFT JOIN classifications c"
        " ON c.repo = i.repo AND c.number = i.number"
        " WHERE i.is_pr = 0 AND i.state = 'OPEN'"
        " GROUP BY i.repo ORDER BY i.repo"
    ).fetchall()
    out = [
        {
            "repo": r["repo"],
            "open": r["open"],
            "classified": r["classified"],
            "pct": 100.0 * r["classified"] / r["open"],
        }
        for r in rows
    ]
    if out:
        total_open = sum(r["open"] for r in out)
        total_classified = sum(r["classified"] for r in out)
        out.append(
            {
                "repo": "TOTAL",
                "open": total_open,
                "classified": total_classified,
                "pct": 100.0 * total_classified / total_open,
            }
        )
    return out


def weekly_throughput(decisions_dir: str | pathlib.Path) -> list[dict]:
    """Decisions recorded per ISO week, from decided_at."""
    counts = Counter(
        _iso_week(r["decided_at"])
        for r in iter_jsonl_records(decisions_dir)
        if r.get("decided_at")
    )
    return [{"week": week, "decided": n} for week, n in sorted(counts.items())]


def category_precision(decisions_dir: str | pathlib.Path) -> list[dict]:
    """Per action type: verdict counts and approval rate among judged rows.

    Skips are shown but excluded from the rate — a skip is "not judged",
    not "wrong".
    """
    by_action: dict[str, Counter] = {}
    for r in iter_jsonl_records(decisions_dir):
        if r.get("action"):
            by_action.setdefault(r["action"], Counter())[r.get("verdict")] += 1
    out = []
    for action in sorted(by_action):
        verdicts = by_action[action]
        judged = verdicts["approved"] + verdicts["rejected"]
        out.append(
            {
                "action": action,
                "approved": verdicts["approved"],
                "rejected": verdicts["rejected"],
                "skipped": verdicts["skipped"],
                "precision": verdicts["approved"] / judged if judged else None,
            }
        )
    return out


def stage_spend(con: sqlite3.Connection) -> list[dict]:
    """Token and USD totals per pipeline stage, biggest spender first."""
    rows = con.execute(
        "SELECT stage, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
        " SUM(cached_tokens) AS cached_tokens, SUM(output_tokens) AS output_tokens,"
        " SUM(usd) AS usd FROM spend GROUP BY stage ORDER BY usd DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def svg_line_chart(
    series: dict[str, list[tuple[str, float]]],
    *,
    width: int = 760,
    height: int = 180,
) -> str:
    """One or two line series as an inline SVG string.

    Each series is a list of (x_label, y) points, assumed to share the same
    x ordering. Renders recessive gridlines, min/max y labels, first/last
    x labels, and a direct label at each line's end.
    """
    series = {name: pts for name, pts in series.items() if pts}
    if not series:
        return "<p class='text-muted'>no data</p>"

    pad_left, pad_right, pad_top, pad_bottom = 40, 90, 10, 22
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    y_max = max(y for pts in series.values() for _, y in pts)
    y_max = y_max or 1  # all-zero series still needs a scale
    n_max = max(len(pts) for pts in series.values())
    x_step = plot_w / max(n_max - 1, 1)

    def sx(i: int) -> float:
        return pad_left + i * x_step

    def sy(y: float) -> float:
        return pad_top + plot_h * (1 - y / y_max)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img"'
        f' style="max-width: {width}px; font: 11px sans-serif;">'
    ]
    for frac in (0.0, 0.5, 1.0):
        gy = pad_top + plot_h * frac
        parts.append(
            f'<line x1="{pad_left}" y1="{gy:.1f}" x2="{pad_left + plot_w}"'
            f' y2="{gy:.1f}" stroke="#e5e5e3" stroke-width="1"/>'
        )
        label = y_max * (1 - frac)
        parts.append(
            f'<text x="{pad_left - 6}" y="{gy + 4:.1f}" text-anchor="end"'
            f' fill="#6b6b68">{label:g}</text>'
        )
    for color, (name, pts) in zip(SERIES_COLORS, sorted(series.items())):
        coords = " ".join(f"{sx(i):.1f},{sy(y):.1f}" for i, (_, y) in enumerate(pts))
        parts.append(
            f'<polyline points="{coords}" fill="none" stroke="{color}"'
            f' stroke-width="2"/>'
        )
        end_x, end_y = sx(len(pts) - 1), sy(pts[-1][1])
        parts.append(
            f'<text x="{end_x + 6:.1f}" y="{end_y + 4:.1f}"'
            f' fill="#3d3d3a">{html.escape(name)}</text>'
        )
    first_pts = next(iter(series.values()))
    for i, anchor in ((0, "start"), (len(first_pts) - 1, "end")):
        parts.append(
            f'<text x="{sx(i):.1f}" y="{height - 6}" text-anchor="{anchor}"'
            f' fill="#6b6b68">{html.escape(str(first_pts[i][0]))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
