# src/triage_verse/review_app/app.py
"""Standalone Shiny app: human review queue for triage-verse proposals.

Run with: shiny run src/triage_verse/review_app/app.py
"""

from __future__ import annotations

import os
import pathlib
from typing import Callable

from shiny import App, Inputs, Outputs, Session, module, reactive, render, ui

from triage_verse import analytics, dashboard, db, decisions, review_queue

DB_PATH = os.environ.get("TRIAGE_VERSE_DB", ".data/mirror.sqlite")
PROPOSALS_DIR = os.environ.get("TRIAGE_VERSE_PROPOSALS", ".data/proposals")
DECISIONS_DIR = os.environ.get("TRIAGE_VERSE_DECISIONS", ".data/decisions")

pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_con = db.connect(DB_PATH)


def _row_label(proposal: dict) -> str:
    return f"{proposal['repo']}#{proposal['issue']} — {proposal['action']}: {proposal['params']}"


def _row_snippet(proposal: dict) -> str:
    issue = db.get_issue(_con, proposal["repo"], proposal["issue"])
    if issue is None:
        return "(not found in mirror)"
    return review_queue.issue_snippet(issue["title"], issue["body"])


@module.ui
def row_ui(proposal: dict, snippet: str):
    github_url = f"https://github.com/{proposal['repo']}/issues/{proposal['issue']}"
    return ui.card(
        ui.card_header(ui.a(_row_label(proposal), href=github_url, target="_blank")),
        ui.p(f"confidence: {proposal.get('confidence', 0.0):.2f}"),
        ui.p(proposal.get("rationale") or ""),
        ui.pre(snippet),
        ui.div(
            ui.input_action_button(
                "approve", "Approve", style="background-color: #2e7d32; color: white;"
            ),
            ui.input_action_button(
                "reject", "Reject", style="background-color: #c62828; color: white;"
            ),
            ui.input_action_button(
                "skip", "Skip", style="background-color: #757575; color: white;"
            ),
            style="display: flex; gap: 0.5rem;",
        ),
    )


@module.server
def row_server(
    input: Inputs,
    output: Outputs,
    session: Session,
    proposal: dict,
    on_decide: Callable[[dict, str], None],
):
    @reactive.effect
    @reactive.event(input.approve)
    def _approve():
        on_decide(proposal, "approved")

    @reactive.effect
    @reactive.event(input.reject)
    def _reject():
        on_decide(proposal, "rejected")

    @reactive.effect
    @reactive.event(input.skip)
    def _skip():
        on_decide(proposal, "skipped")


def _table(rows: list[dict], columns: list[str], format_row=None) -> ui.Tag:
    if not rows:
        return ui.p("no data", class_="text-muted")
    format_row = format_row or (lambda r: [r[c] for c in columns])
    return ui.tags.table(
        ui.tags.thead(ui.tags.tr(*[ui.tags.th(c) for c in columns])),
        ui.tags.tbody(
            *[ui.tags.tr(*[ui.tags.td(v) for v in format_row(r)]) for r in rows]
        ),
        class_="table table-sm w-auto",
    )


def _stat_card(label: str, value_id: str) -> ui.Tag:
    return ui.card(
        ui.p(label, class_="text-muted mb-1"),
        ui.output_text(value_id, inline=True),
        style="font-size: 1.5rem;",
    )


_repo_choices = ["All repos"] + [
    r["repo"]
    for r in _con.execute(
        "SELECT DISTINCT repo FROM issues WHERE is_pr=0 ORDER BY repo"
    )
]

dashboard_panel = ui.nav_panel(
    "Dashboard",
    ui.layout_columns(
        _stat_card("Queue depth", "stat_queue_depth"),
        _stat_card("Open issues", "stat_open_issues"),
        _stat_card("Triage coverage", "stat_coverage"),
        _stat_card("Total spend", "stat_spend"),
        col_widths=[3, 3, 3, 3],
    ),
    ui.input_select("dash_repo", "Repo", choices=_repo_choices),
    ui.h4("Backlog burndown (open issues at start of week)"),
    ui.output_ui("burndown_ui"),
    ui.h4("Opened vs closed per week"),
    ui.output_ui("flux_ui"),
    ui.h4("Close-reason mix"),
    ui.output_ui("close_reasons_ui"),
    ui.h4("Triage coverage (open issues with a classification)"),
    ui.output_ui("coverage_ui"),
    ui.h4("Review throughput (decisions per week)"),
    ui.output_ui("throughput_ui"),
    ui.h4("Per-category precision (approval rate of judged proposals)"),
    ui.output_ui("precision_ui"),
    ui.h4("Spend per stage"),
    ui.output_ui("spend_ui"),
)

app_ui = ui.page_navbar(
    ui.nav_panel(
        "Queue",
        ui.input_action_button("approve_visible", "Approve visible rows"),
        ui.output_ui("queue_ui"),
    ),
    dashboard_panel,
    title="Triage review",
)


def server(input: Inputs, output: Outputs, session: Session):
    queue = reactive.value(
        review_queue.load_undecided(PROPOSALS_DIR, DECISIONS_DIR, _con)
    )
    wired: set[str] = set()

    def refresh() -> None:
        queue.set(review_queue.load_undecided(PROPOSALS_DIR, DECISIONS_DIR, _con))

    def on_decide(proposal: dict, verdict: str) -> None:
        decisions.write([decisions.record(proposal, verdict)], DECISIONS_DIR)
        refresh()

    @render.ui
    def queue_ui():
        rows = queue.get()
        if not rows:
            return ui.p("Queue empty — nothing to review.")
        cards = []
        for proposal in rows:
            row_id = proposal["id"]
            if row_id not in wired:
                row_server(row_id, proposal=proposal, on_decide=on_decide)
                wired.add(row_id)
            cards.append(row_ui(row_id, proposal, _row_snippet(proposal)))
        return ui.div(*cards)

    @reactive.effect
    @reactive.event(input.approve_visible)
    def _approve_visible():
        decisions.write(
            [decisions.record(p, "approved") for p in queue.get()], DECISIONS_DIR
        )
        refresh()

    # --- Dashboard tab ---

    def _dash_repo() -> str | None:
        choice = input.dash_repo()
        return None if choice == "All repos" else choice

    @render.text
    def stat_queue_depth():
        return str(len(queue.get()))

    @render.text
    def stat_open_issues():
        coverage = dashboard.triage_coverage(_con)
        return str(coverage[-1]["open"]) if coverage else "0"

    @render.text
    def stat_coverage():
        coverage = dashboard.triage_coverage(_con)
        return f"{coverage[-1]['pct']:.0f}%" if coverage else "—"

    @render.text
    def stat_spend():
        total = sum(r["usd"] for r in dashboard.stage_spend(_con))
        return f"${total:,.2f}"

    @render.ui
    def burndown_ui():
        series = analytics.weekly_open_counts(_con, repo=_dash_repo())
        return ui.HTML(
            dashboard.svg_line_chart({"open": [(r["week"], r["open"]) for r in series]})
        )

    @render.ui
    def flux_ui():
        series = analytics.weekly_flux(_con, repo=_dash_repo())
        return ui.HTML(
            dashboard.svg_line_chart(
                {
                    "opened": [(r["week"], r["opened"]) for r in series],
                    "closed": [(r["week"], r["closed"]) for r in series],
                }
            )
        )

    @render.ui
    def close_reasons_ui():
        mix = analytics.close_reason_mix(_con, repo=_dash_repo())
        rows = [{"reason": k, "count": v} for k, v in sorted(mix.items())]
        return _table(rows, ["reason", "count"])

    @render.ui
    def coverage_ui():
        return _table(
            dashboard.triage_coverage(_con),
            ["repo", "open", "classified", "pct"],
            lambda r: [r["repo"], r["open"], r["classified"], f"{r['pct']:.1f}%"],
        )

    @render.ui
    def throughput_ui():
        series = dashboard.weekly_throughput(DECISIONS_DIR)
        return ui.HTML(
            dashboard.svg_line_chart(
                {"decided": [(r["week"], r["decided"]) for r in series]}
            )
        )

    @render.ui
    def precision_ui():
        return _table(
            dashboard.category_precision(DECISIONS_DIR),
            ["action", "approved", "rejected", "skipped", "precision"],
            lambda r: [
                r["action"],
                r["approved"],
                r["rejected"],
                r["skipped"],
                "—" if r["precision"] is None else f"{r['precision']:.0%}",
            ],
        )

    @render.ui
    def spend_ui():
        return _table(
            dashboard.stage_spend(_con),
            ["stage", "calls", "input_tokens", "cached_tokens", "output_tokens", "usd"],
            lambda r: [
                r["stage"],
                r["calls"],
                f"{r['input_tokens']:,}",
                f"{r['cached_tokens']:,}",
                f"{r['output_tokens']:,}",
                f"${r['usd']:,.2f}",
            ],
        )


app = App(app_ui, server)
