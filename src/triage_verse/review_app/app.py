# src/triage_verse/review_app/app.py
"""Standalone Shiny app: human review queue for triage-verse proposals.

Run with: shiny run src/triage_verse/review_app/app.py
"""

from __future__ import annotations

import os
import pathlib
from typing import Callable

from shiny import App, Inputs, Outputs, Session, module, reactive, render, ui

from triage_verse import analytics, dashboard, db, decisions, drawer, review_queue

DB_PATH = os.environ.get("TRIAGE_VERSE_DB", ".data/mirror.sqlite")
PROPOSALS_DIR = os.environ.get("TRIAGE_VERSE_PROPOSALS", ".data/proposals")
DECISIONS_DIR = os.environ.get("TRIAGE_VERSE_DECISIONS", ".data/decisions")

pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_con = db.connect(DB_PATH)

_DRAWER_CSS = """
#drawer-backdrop {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); z-index: 1040;
}
#drawer-panel {
  position: fixed; top: 0; right: 0; height: 100vh; width: min(640px, 95vw);
  background: white; z-index: 1050; overflow-y: auto; padding: 1rem 1.25rem;
  box-shadow: -0.25rem 0 1rem rgba(0, 0, 0, 0.2);
  animation: drawer-slide-in 0.15s ease-out;
}
@keyframes drawer-slide-in {
  from { transform: translateX(100%); }
  to { transform: none; }
}
.drawer-meta { color: #57606a; font-size: 0.875rem; }
.drawer-label {
  display: inline-block; border: 1px solid #d0d7de; border-radius: 999px;
  padding: 0 0.5rem; margin-right: 0.25rem; font-size: 0.8rem;
}
.drawer-comment {
  border: 1px solid #d0d7de; border-radius: 0.375rem;
  padding: 0.5rem 0.75rem; margin-bottom: 0.75rem;
}
"""


def _row_label(proposal: dict) -> str:
    return f"{proposal['repo']}#{proposal['issue']} — {proposal['action']}: {proposal['params']}"


def _row_snippet(proposal: dict) -> str:
    issue = db.get_issue(_con, proposal["repo"], proposal["issue"])
    if issue is None:
        return "(not found in mirror)"
    return review_queue.issue_snippet(issue["title"], issue["body"])


@module.ui
def row_ui(proposal: dict, snippet: str):
    return ui.card(
        ui.card_header(ui.input_action_link("open", _row_label(proposal))),
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
    on_open: Callable[[dict], None],
):
    @reactive.effect
    @reactive.event(input.open)
    def _open():
        on_open(proposal)

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


def _drawer_meta_line(item: dict) -> str:
    kind = "PR" if item["is_pr"] else "Issue"
    state = item["state"]
    if item["pr"] and item["pr"]["merged"]:
        state = "MERGED"
    elif item["state_reason"]:
        state = f"{state} ({item['state_reason']})"
    author = item["author"] or "unknown"
    return f"{kind} · {state} · opened by {author} on {item['created_at'][:10]}"


def _drawer_comments(item: dict) -> list:
    parts = [ui.h4(f"Comments ({len(item['comments'])})")]
    if item["comment_count"] > len(item["comments"]):
        parts.append(
            ui.p(
                f"{item['comment_count']} comments on GitHub; "
                f"{len(item['comments'])} mirrored.",
                class_="drawer-meta",
            )
        )
    if not item["comments"]:
        parts.append(ui.p("(no comments)"))
    for c in item["comments"]:
        parts.append(
            ui.div(
                ui.p(
                    f"{c['author'] or 'unknown'} · {c['created_at'][:10]}",
                    class_="drawer-meta",
                ),
                ui.markdown(c["body"] or ""),
                class_="drawer-comment",
            )
        )
    return parts


def _drawer_proposal(proposal: dict) -> list:
    return [
        ui.h4("Proposal"),
        ui.p(f"{proposal['action']}: {proposal['params']}"),
        ui.p(f"confidence: {proposal.get('confidence', 0.0):.2f}"),
        ui.p(proposal.get("rationale") or "(no rationale)"),
        ui.h4("Linked evidence"),
        ui.tags.ul(
            *[
                ui.tags.li(ui.a(url, href=url, target="_blank"))
                for url in proposal.get("evidence", [])
            ]
        ),
    ]


def _drawer_panel(state: dict, item: dict | None):
    parts = [
        ui.div(
            ui.input_action_button("drawer_close", "Close"),
            style="display: flex; justify-content: flex-end;",
        )
    ]
    if item is None:
        github_url = f"https://github.com/{state['repo']}/issues/{state['number']}"
        parts += [
            ui.h3(f"{state['repo']}#{state['number']}"),
            ui.p("(not found in mirror)"),
        ]
    else:
        github_url = item["github_url"]
        parts += [
            ui.h3(item["title"]),
            ui.p(_drawer_meta_line(item), class_="drawer-meta"),
            ui.div(
                *[ui.span(label, class_="drawer-label") for label in item["labels"]]
            ),
            ui.p(
                " · ".join(
                    bit
                    for bit in (
                        f"milestone: {item['milestone']}" if item["milestone"] else "",
                        f"assignees: {', '.join(item['assignees'])}"
                        if item["assignees"]
                        else "",
                        f"reactions: {item['reaction_count']}"
                        if item["reaction_count"]
                        else "",
                    )
                    if bit
                ),
                class_="drawer-meta",
            ),
            ui.markdown(item["body"] or "(no description)"),
            *_drawer_comments(item),
        ]
    parts += _drawer_proposal(state["proposal"])
    parts.append(ui.p(ui.a("Open on GitHub ↗", href=github_url, target="_blank")))
    return ui.tags.div(*parts, id="drawer-panel")


app_ui = ui.page_navbar(
    ui.nav_panel(
        "Queue",
        ui.input_action_button("approve_visible", "Approve visible rows"),
        ui.output_ui("queue_ui"),
        ui.output_ui("drawer_ui"),
    ),
    dashboard_panel,
    title="Triage review",
    header=ui.tags.style(_DRAWER_CSS),
)


def server(input: Inputs, output: Outputs, session: Session):
    queue = reactive.value(
        review_queue.load_undecided(PROPOSALS_DIR, DECISIONS_DIR, _con)
    )
    drawer_state = reactive.value[dict | None](None)
    wired: set[str] = set()

    def refresh() -> None:
        queue.set(review_queue.load_undecided(PROPOSALS_DIR, DECISIONS_DIR, _con))

    def on_decide(proposal: dict, verdict: str) -> None:
        decisions.write([decisions.record(proposal, verdict)], DECISIONS_DIR)
        state = drawer_state.get()
        if state is not None and state["proposal"]["id"] == proposal["id"]:
            drawer_state.set(None)
        refresh()

    def on_open(proposal: dict) -> None:
        drawer_state.set(
            {
                "repo": proposal["repo"],
                "number": proposal["issue"],
                "proposal": proposal,
            }
        )

    @render.ui
    def queue_ui():
        rows = queue.get()
        if not rows:
            return ui.p("Queue empty — nothing to review.")
        cards = []
        for proposal in rows:
            row_id = proposal["id"]
            if row_id not in wired:
                row_server(
                    row_id, proposal=proposal, on_decide=on_decide, on_open=on_open
                )
                wired.add(row_id)
            cards.append(row_ui(row_id, proposal, _row_snippet(proposal)))
        return ui.div(*cards)

    @render.ui
    def drawer_ui():
        state = drawer_state.get()
        if state is None:
            return None
        item = drawer.load_item(_con, state["repo"], state["number"])
        return ui.div(
            ui.tags.div(
                id="drawer-backdrop",
                onclick="document.getElementById('drawer_close').click();",
            ),
            _drawer_panel(state, item),
        )

    @reactive.effect
    @reactive.event(input.drawer_close)
    def _drawer_close():
        drawer_state.set(None)

    @reactive.effect
    @reactive.event(input.approve_visible)
    def _approve_visible():
        decisions.write(
            [decisions.record(p, "approved") for p in queue.get()], DECISIONS_DIR
        )
        drawer_state.set(None)
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
            ["action", "approved", "edited", "rejected", "skipped", "precision"],
            lambda r: [
                r["action"],
                r["approved"],
                r["edited"],
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
