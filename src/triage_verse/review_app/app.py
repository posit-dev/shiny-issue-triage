# src/triage_verse/review_app/app.py
"""Standalone Shiny app: human review queue for triage-verse proposals.

Run with: shiny run src/triage_verse/review_app/app.py
"""

from __future__ import annotations

import os
import pathlib
from typing import Callable

from shiny import App, Inputs, Outputs, Session, module, reactive, render, ui

from triage_verse import db, decisions, drawer, review_queue

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


app_ui = ui.page_fluid(
    ui.tags.style(_DRAWER_CSS),
    ui.h2("Triage review queue"),
    ui.input_action_button("approve_visible", "Approve visible rows"),
    ui.output_ui("queue_ui"),
    ui.output_ui("drawer_ui"),
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


app = App(app_ui, server)
