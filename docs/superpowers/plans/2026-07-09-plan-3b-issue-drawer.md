# Plan 3b: Issue/PR Slide-Over Drawer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A GitHub-Projects-style slide-over drawer in the review app that renders the full issue/PR (title, body, comments, labels, state, proposal evidence) from the local mirror, with a deep link out to GitHub.

**Architecture:** A pure `drawer.py` module assembles the full item dict from `mirror.sqlite` (via two new read helpers in `db.py`); the plain-Shiny review app holds one app-level drawer whose visibility is a reactive value set by clicking any queue row's title. No network calls, no React, no new dependencies.

**Tech Stack:** Python 3.12+ (uv-managed), sqlite3 + sqlite-vec mirror, Shiny for Python (server-rendered), pytest.

**Spec:** `docs/superpowers/specs/2026-07-09-plan-3b-issue-drawer-design.md`

## Global Constraints

- Run everything with `uv run` (the system Python's sqlite3 lacks extension loading; `db.connect` raises without it).
- Pure modules (`db.py`, `drawer.py`) must not import Shiny; the app stays a thin wire-up (Plan 3a convention).
- Tests are offline: in-memory/tmp-path SQLite fixtures only, no network, no Shiny test harness.
- 1:1 module/test naming: `src/triage_verse/<name>.py` ↔ `tests/triage_verse/test_<name>.py`.
- The drawer renders only mirror data; the single outbound affordance is the GitHub deep link (`/pull/N` for PRs, `/issues/N` otherwise).

---

### Task 1: Mirror read helpers `db.get_comments` and `db.get_pr`

**Files:**
- Modify: `src/triage_verse/db.py` (after `get_issue`, around line 229)
- Test: `tests/triage_verse/test_db.py`

**Interfaces:**
- Consumes: existing `db.connect`, `db.upsert_issue`, `db.upsert_pr`, `db.upsert_comment` and the `_issue_row` fixture helper already in `test_db.py`.
- Produces: `get_comments(con: sqlite3.Connection, repo: str, issue_number: int) -> list[sqlite3.Row]` (ordered by `created_at`, then `comment_id`); `get_pr(con: sqlite3.Connection, repo: str, number: int) -> sqlite3.Row | None`.

- [x] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_db.py`:

```python
def _comment_row(**overrides):
    row = {
        "repo": "rstudio/shiny",
        "issue_number": 1,
        "comment_id": 1,
        "author": "bob",
        "body": "hi",
        "created_at": "2024-01-03T00:00:00Z",
        "updated_at": "2024-01-03T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_get_comments_ordered_and_filtered(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_comment(
        con, _comment_row(comment_id=2, created_at="2024-01-05T00:00:00Z", body="second")
    )
    db.upsert_comment(con, _comment_row(comment_id=1, body="first"))
    db.upsert_comment(
        con, _comment_row(comment_id=3, issue_number=9, body="other issue")
    )
    db.upsert_comment(
        con, _comment_row(comment_id=4, repo="rstudio/bslib", body="other repo")
    )

    comments = db.get_comments(con, "rstudio/shiny", 1)
    assert [c["body"] for c in comments] == ["first", "second"]


def test_get_comments_empty(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert db.get_comments(con, "rstudio/shiny", 1) == []


def test_get_pr_roundtrip_and_missing(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_pr(
        con,
        {
            "repo": "rstudio/shiny",
            "number": 7,
            "merged": 1,
            "merged_at": "2024-03-01T00:00:00Z",
            "closing_issue_refs_json": "[3]",
            "head_ref": "fix",
            "base_ref": "main",
        },
    )
    assert db.get_pr(con, "rstudio/shiny", 7)["merged"] == 1
    assert db.get_pr(con, "rstudio/shiny", 999) is None
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_db.py -v -k "get_comments or get_pr"`
Expected: FAIL with `AttributeError: module 'triage_verse.db' has no attribute 'get_comments'` (and same for `get_pr`).

- [x] **Step 3: Write minimal implementation**

In `src/triage_verse/db.py`, directly after `get_issue`:

```python
def get_pr(con: sqlite3.Connection, repo: str, number: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM prs WHERE repo=? AND number=?", (repo, number)
    ).fetchone()


def get_comments(
    con: sqlite3.Connection, repo: str, issue_number: int
) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM comments WHERE repo=? AND issue_number=?"
        " ORDER BY created_at, comment_id",
        (repo, issue_number),
    ).fetchall()
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_db.py -v`
Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add src/triage_verse/db.py tests/triage_verse/test_db.py
git commit -m "feat(db): add get_pr and get_comments read helpers"
```

---

### Task 2: Pure drawer data module `drawer.py`

**Files:**
- Create: `src/triage_verse/drawer.py`
- Test: `tests/triage_verse/test_drawer.py`

**Interfaces:**
- Consumes: `db.get_issue`, `db.get_pr(con, repo, number) -> sqlite3.Row | None`, `db.get_comments(con, repo, issue_number) -> list[sqlite3.Row]` from Task 1.
- Produces: `load_item(con: sqlite3.Connection, repo: str, number: int) -> dict | None`. `None` when the issue is missing from the mirror. Otherwise a dict with all `issues` columns except that `labels_json`/`assignees_json` are replaced by parsed `labels: list[str]` and `assignees: list[str]`, plus `comments: list[dict]` (keys `author`, `body`, `created_at`; oldest first), `pr: dict | None` (keys `merged: bool`, `merged_at`, `head_ref`, `base_ref`; only for `is_pr` rows with a `prs` record), and `github_url: str` (`/pull/N` when `is_pr`, else `/issues/N`).

- [x] **Step 1: Write the failing tests**

Create `tests/triage_verse/test_drawer.py`:

```python
from triage_verse import db, drawer


def _issue_row(**overrides):
    row = {
        "repo": "rstudio/shiny",
        "number": 1,
        "title": "first",
        "body": "body",
        "state": "OPEN",
        "state_reason": None,
        "author": "alice",
        "labels_json": "[]",
        "assignees_json": "[]",
        "milestone": None,
        "comment_count": 0,
        "reaction_count": 0,
        "is_pr": 0,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "closed_at": None,
    }
    row.update(overrides)
    return row


def _comment_row(**overrides):
    row = {
        "repo": "rstudio/shiny",
        "issue_number": 1,
        "comment_id": 1,
        "author": "bob",
        "body": "hi",
        "created_at": "2024-01-03T00:00:00Z",
        "updated_at": "2024-01-03T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_load_item_full_issue(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(
        con,
        _issue_row(
            labels_json='["bug", "P1"]',
            assignees_json='["carol"]',
            comment_count=2,
        ),
    )
    db.upsert_comment(
        con, _comment_row(comment_id=2, created_at="2024-01-05T00:00:00Z", body="later")
    )
    db.upsert_comment(con, _comment_row(comment_id=1, body="earlier"))

    item = drawer.load_item(con, "rstudio/shiny", 1)
    assert item["title"] == "first"
    assert item["labels"] == ["bug", "P1"]
    assert item["assignees"] == ["carol"]
    assert "labels_json" not in item
    assert "assignees_json" not in item
    assert [c["body"] for c in item["comments"]] == ["earlier", "later"]
    assert item["comments"][0] == {
        "author": "bob",
        "body": "earlier",
        "created_at": "2024-01-03T00:00:00Z",
    }
    assert item["pr"] is None
    assert item["github_url"] == "https://github.com/rstudio/shiny/issues/1"


def test_load_item_missing_returns_none(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert drawer.load_item(con, "rstudio/shiny", 999) is None


def test_load_item_pr_with_metadata(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row(number=7, is_pr=1, state="CLOSED"))
    db.upsert_pr(
        con,
        {
            "repo": "rstudio/shiny",
            "number": 7,
            "merged": 1,
            "merged_at": "2024-03-01T00:00:00Z",
            "closing_issue_refs_json": "[3]",
            "head_ref": "fix",
            "base_ref": "main",
        },
    )

    item = drawer.load_item(con, "rstudio/shiny", 7)
    assert item["github_url"] == "https://github.com/rstudio/shiny/pull/7"
    assert item["pr"] == {
        "merged": True,
        "merged_at": "2024-03-01T00:00:00Z",
        "head_ref": "fix",
        "base_ref": "main",
    }


def test_load_item_pr_without_prs_row(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row(number=8, is_pr=1))
    item = drawer.load_item(con, "rstudio/shiny", 8)
    assert item["pr"] is None
    assert item["github_url"] == "https://github.com/rstudio/shiny/pull/8"


def test_load_item_empty_body_and_no_comments(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row(body=None))
    item = drawer.load_item(con, "rstudio/shiny", 1)
    assert item["body"] is None
    assert item["comments"] == []
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_drawer.py -v`
Expected: FAIL at import with `ImportError: cannot import name 'drawer'`.

- [x] **Step 3: Write minimal implementation**

Create `src/triage_verse/drawer.py`:

```python
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
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_drawer.py -v`
Expected: all PASS.

- [x] **Step 5: Commit**

```bash
git add src/triage_verse/drawer.py tests/triage_verse/test_drawer.py
git commit -m "feat: pure drawer module assembling full issue/PR from mirror"
```

---

### Task 3: Wire the slide-over drawer into the review app

**Files:**
- Modify: `src/triage_verse/review_app/app.py`

**Interfaces:**
- Consumes: `drawer.load_item(con, repo, number) -> dict | None` from Task 2; existing `review_queue`, `decisions` modules.
- Produces: no new Python interfaces — UI behavior only. Row titles open the drawer; the drawer shows the full item plus the proposal's evidence; Close button and backdrop click dismiss it; deciding the open row's proposal also dismisses it.

- [x] **Step 1: Rewrite `src/triage_verse/review_app/app.py`**

The full file after this task (changes from Plan 3a: `_DRAWER_CSS` block, row header becomes an `input_action_link` wired to `on_open`, new `_drawer_panel` builder and `drawer_ui` output, `drawer_state` reactive value, decide-closes-drawer rule):

```python
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
    drawer_state = reactive.value(None)
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
            {"repo": proposal["repo"], "number": proposal["issue"], "proposal": proposal}
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
                row_server(row_id, proposal=proposal, on_decide=on_decide, on_open=on_open)
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
```

- [x] **Step 2: Run the full test suite and an import smoke check**

Run: `uv run pytest` — Expected: all PASS, no regressions.
Run: `uv run python -c "from triage_verse.review_app.app import app; print(type(app))"` — Expected: `<class 'shiny._app.App'>` (module imports and the App constructs).

- [x] **Step 3: Manual verification against seeded data**

Seed a throwaway `.data/` (tmp dir) with a mirror containing an issue + comments and a matching proposal JSONL, then `TRIAGE_VERSE_DB=... TRIAGE_VERSE_PROPOSALS=... TRIAGE_VERSE_DECISIONS=... uv run shiny run src/triage_verse/review_app/app.py` and check: clicking a row title slides the drawer in with title/state/labels/body/comments/proposal/evidence; Close and backdrop both dismiss; deciding the open row dismisses; a proposal whose issue is missing from the mirror shows the "not found in mirror" drawer; a PR row deep-links to `/pull/N`.

- [x] **Step 4: Commit**

```bash
git add src/triage_verse/review_app/app.py
git commit -m "feat(review-app): issue/PR slide-over drawer"
```
