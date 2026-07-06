# src/triage_verse/review_app/app.py
"""Standalone Shiny app: human review queue for triage-verse proposals.

Run with: shiny run src/triage_verse/review_app/app.py
"""

from __future__ import annotations

import os
import pathlib
from typing import Callable

from shiny import App, Inputs, Outputs, Session, module, reactive, render, ui

from triage_verse import db, decisions, review_queue

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


app_ui = ui.page_fluid(
    ui.h2("Triage review queue"),
    ui.input_action_button("approve_visible", "Approve visible rows"),
    ui.output_ui("queue_ui"),
)


def server(input: Inputs, output: Outputs, session: Session):
    queue = reactive.value(review_queue.load_undecided(PROPOSALS_DIR, DECISIONS_DIR))
    wired: set[str] = set()

    def refresh() -> None:
        queue.set(review_queue.load_undecided(PROPOSALS_DIR, DECISIONS_DIR))

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


app = App(app_ui, server)
