# Plan 3d — Review close / close-duplicate proposals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put `close` and `close-duplicate` proposals into the review queue with an evidence-before-approval guardrail: close-action rows can only be approved/rejected from the full-evidence drawer, bulk approve stays scoped to label/priority proposals, and the drawer shows the duplicate's sibling issue.

**Architecture:** Two pure additions to `review_queue.py` (widened `SUPPORTED_ACTIONS` + `HIGH_STAKES_ACTIONS`, and a `duplicate_sibling` evidence-URL parser), then thin Shiny wiring in `review_app/app.py`: conditional row buttons, app-level drawer decide buttons feeding the existing `on_decide`, a sibling summary block in the drawer's Proposal section, and a filtered bulk approve.

**Tech Stack:** Python 3.11+, Shiny for Python (server-rendered, no React), sqlite3 mirror, pytest via `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-07-09-plan-3d-close-review-design.md`

## Global Constraints

- No changes to the Plan 2 proposal schema or existing JSONL records; the review layer derives the sibling from evidence URLs.
- Nothing writes to GitHub; approving a close only appends a decision record (executor is Plan 4).
- `review_queue.py` stays free of Shiny imports (pure, offline-testable).
- App wiring (`review_app/app.py`) is verified manually, not unit-tested — same stance as Plans 3a–3c.
- CI gates: `make py-check-format`, `make py-check-types` (pyright), `make py-check-tests` (`uv run pytest`).
- Conventional-commit messages (CI-enforced).

---

### Task 1: Widen supported actions and define the high-stakes set

**Files:**
- Modify: `src/triage_verse/review_queue.py:14` (the `SUPPORTED_ACTIONS` constant)
- Test: `tests/triage_verse/test_review_queue.py`

**Interfaces:**
- Produces: `review_queue.SUPPORTED_ACTIONS == frozenset({"add-label", "set-priority", "close", "close-duplicate"})` and `review_queue.HIGH_STAKES_ACTIONS == frozenset({"close", "close-duplicate"})`. Task 3 and Task 4 consult `HIGH_STAKES_ACTIONS` from the app.

- [ ] **Step 1: Rewrite the out-of-scope test and add inclusion + subset tests**

In `tests/triage_verse/test_review_queue.py`, replace the existing `test_load_undecided_excludes_out_of_scope_actions` (it currently asserts `close`/`close-duplicate` are excluded — that behavior is being inverted) with:

```python
def test_load_undecided_includes_close_actions(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.5,
            },
            {
                "id": "b",
                "repo": "r/r",
                "issue": 2,
                "action": "close",
                "confidence": 0.7,
            },
            {
                "id": "c",
                "repo": "r/r",
                "issue": 3,
                "action": "close-duplicate",
                "confidence": 0.6,
            },
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, _mirror(tmp_path))
    assert [r["id"] for r in rows] == ["b", "c", "a"]


def test_load_undecided_excludes_out_of_scope_actions(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.5,
            },
            {
                "id": "b",
                "repo": "r/r",
                "issue": 2,
                "action": "transfer",
                "confidence": 0.5,
            },
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, _mirror(tmp_path))
    assert [r["id"] for r in rows] == ["a"]


def test_close_proposal_leaves_queue_when_issue_closes(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "close",
                "confidence": 0.9,
            }
        ],
    )
    con = _mirror(tmp_path)
    _seed_issue(con, "r/r", 1, "CLOSED")
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, con)
    assert rows == []


def test_high_stakes_actions_are_supported():
    assert review_queue.HIGH_STAKES_ACTIONS <= review_queue.SUPPORTED_ACTIONS
    assert review_queue.HIGH_STAKES_ACTIONS == frozenset({"close", "close-duplicate"})
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -v`
Expected: `test_load_undecided_includes_close_actions` FAILS (close actions filtered out), `test_high_stakes_actions_are_supported` FAILS with `AttributeError: ... has no attribute 'HIGH_STAKES_ACTIONS'`; the other two PASS (existing behavior).

- [ ] **Step 3: Widen the constants in `review_queue.py`**

Replace line 14 of `src/triage_verse/review_queue.py`:

```python
SUPPORTED_ACTIONS = frozenset({"add-label", "set-priority", "close", "close-duplicate"})
# Actions that must be judged from the full-evidence drawer, never a row snippet
# or bulk approve.
HIGH_STAKES_ACTIONS = frozenset({"close", "close-duplicate"})
```

- [ ] **Step 4: Run the full module's tests**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/review_queue.py tests/triage_verse/test_review_queue.py
git commit -m "feat(review-queue): support close and close-duplicate proposals"
```

---

### Task 2: `duplicate_sibling` evidence parser

**Files:**
- Modify: `src/triage_verse/review_queue.py` (append after `issue_snippet`)
- Test: `tests/triage_verse/test_review_queue.py`

**Interfaces:**
- Produces: `review_queue.duplicate_sibling(proposal: dict) -> tuple[str, int] | None` — returns `("owner/repo", number)` for the first evidence URL of the form `https://github.com/{owner}/{repo}/issues/{n}` that is not the proposal's own issue; `None` when evidence is missing, malformed, or self-only. Task 4 calls this from the drawer.

- [ ] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_review_queue.py`:

```python
def test_duplicate_sibling_returns_other_issue():
    proposal = {
        "repo": "r/a",
        "issue": 1,
        "evidence": [
            "https://github.com/r/a/issues/1",
            "https://github.com/r/b/issues/2",
        ],
    }
    assert review_queue.duplicate_sibling(proposal) == ("r/b", 2)


def test_duplicate_sibling_handles_sibling_listed_first():
    proposal = {
        "repo": "r/a",
        "issue": 1,
        "evidence": [
            "https://github.com/r/b/issues/2",
            "https://github.com/r/a/issues/1",
        ],
    }
    assert review_queue.duplicate_sibling(proposal) == ("r/b", 2)


def test_duplicate_sibling_none_when_self_only():
    proposal = {
        "repo": "r/a",
        "issue": 1,
        "evidence": ["https://github.com/r/a/issues/1"],
    }
    assert review_queue.duplicate_sibling(proposal) is None


def test_duplicate_sibling_none_when_evidence_missing():
    assert review_queue.duplicate_sibling({"repo": "r/a", "issue": 1}) is None


def test_duplicate_sibling_skips_malformed_urls():
    proposal = {
        "repo": "r/a",
        "issue": 1,
        "evidence": [
            "not a url",
            "https://github.com/r/b/pull/9",
            "https://github.com/r/b/issues/not-a-number",
            "https://github.com/r/b/issues/2",
        ],
    }
    assert review_queue.duplicate_sibling(proposal) == ("r/b", 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -k duplicate_sibling -v`
Expected: all 5 FAIL with `AttributeError: ... has no attribute 'duplicate_sibling'`.

- [ ] **Step 3: Implement `duplicate_sibling`**

Add to `src/triage_verse/review_queue.py`. `re` must join the existing import block at the top of the file:

```python
import re
```

```python
_EVIDENCE_URL = re.compile(
    r"^https://github\.com/([^/]+/[^/]+)/issues/(\d+)$"
)


def duplicate_sibling(proposal: dict) -> tuple[str, int] | None:
    """The other issue of a close-duplicate pair, from the proposal's evidence URLs."""
    for url in proposal.get("evidence") or []:
        m = _EVIDENCE_URL.match(url)
        if m is None:
            continue
        repo, number = m.group(1), int(m.group(2))
        if (repo, number) != (proposal["repo"], proposal["issue"]):
            return repo, number
    return None
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/review_queue.py tests/triage_verse/test_review_queue.py
git commit -m "feat(review-queue): derive close-duplicate sibling from evidence URLs"
```

---

### Task 3: Row guardrail and low-stakes bulk approve

**Files:**
- Modify: `src/triage_verse/review_app/app.py` (`row_ui` at ~line 61, `_approve_visible` at ~line 340, the Queue nav panel's button at ~line 269)

**Interfaces:**
- Consumes: `review_queue.HIGH_STAKES_ACTIONS` (Task 1).
- Produces: high-stakes rows fire the existing `on_open` from a "Review evidence" button instead of exposing Approve/Reject; `row_server` is unchanged (its `input.approve` / `input.reject` effects simply never fire for high-stakes rows because those buttons don't exist).

No unit tests — app wiring is manually verified (Global Constraints). Steps below are edit → smoke-run → commit.

- [ ] **Step 1: Make `row_ui` conditional on action stakes**

Replace the `row_ui` function in `src/triage_verse/review_app/app.py`:

```python
@module.ui
def row_ui(proposal: dict, snippet: str):
    high_stakes = proposal["action"] in review_queue.HIGH_STAKES_ACTIONS
    header: list = [ui.input_action_link("open", _row_label(proposal))]
    if high_stakes:
        header.insert(
            0,
            ui.span(
                proposal["action"],
                style=(
                    "background-color: #c62828; color: white; border-radius: 999px; "
                    "padding: 0 0.5rem; margin-right: 0.5rem; font-size: 0.8rem;"
                ),
            ),
        )
    if high_stakes:
        buttons = [
            ui.input_action_button(
                "open_evidence",
                "Review evidence",
                style="background-color: #1565c0; color: white;",
            ),
            ui.input_action_button(
                "skip", "Skip", style="background-color: #757575; color: white;"
            ),
        ]
    else:
        buttons = [
            ui.input_action_button(
                "approve", "Approve", style="background-color: #2e7d32; color: white;"
            ),
            ui.input_action_button(
                "reject", "Reject", style="background-color: #c62828; color: white;"
            ),
            ui.input_action_button(
                "skip", "Skip", style="background-color: #757575; color: white;"
            ),
        ]
    return ui.card(
        ui.card_header(*header),
        ui.p(f"confidence: {proposal.get('confidence', 0.0):.2f}"),
        ui.p(proposal.get("rationale") or ""),
        ui.pre(snippet),
        ui.div(*buttons, style="display: flex; gap: 0.5rem;"),
    )
```

- [ ] **Step 2: Wire the `open_evidence` button in `row_server`**

Shiny module effects on inputs whose UI doesn't exist never fire, so `_approve`/`_reject` are safe as-is. Add one effect to `row_server`, next to the existing `_open`:

```python
    @reactive.effect
    @reactive.event(input.open_evidence)
    def _open_evidence():
        on_open(proposal)
```

- [ ] **Step 3: Scope bulk approve to low-stakes rows and relabel the button**

In `app_ui`'s Queue nav panel, change the button label:

```python
        ui.input_action_button("approve_visible", "Approve visible label/priority rows"),
```

Replace the `_approve_visible` effect body in `server`:

```python
    @reactive.effect
    @reactive.event(input.approve_visible)
    def _approve_visible():
        decisions.write(
            [
                decisions.record(p, "approved")
                for p in queue.get()
                if p["action"] not in review_queue.HIGH_STAKES_ACTIONS
            ],
            DECISIONS_DIR,
        )
        drawer_state.set(None)
        refresh()
```

- [ ] **Step 4: Smoke-run the app and the suite**

Run: `uv run pytest && uv run python -c "import triage_verse.review_app.app"`
Expected: tests PASS; the import (which builds the static UI) raises nothing.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/review_app/app.py
git commit -m "feat(review-app): route close-action rows through the evidence drawer"
```

---

### Task 4: Drawer decide buttons and duplicate-sibling block

**Files:**
- Modify: `src/triage_verse/review_app/app.py` (`_drawer_proposal` at ~line 205, its call in `_drawer_panel`, `drawer_ui` and new effects in `server`)

**Interfaces:**
- Consumes: `review_queue.duplicate_sibling` (Task 2), `drawer.load_item(con, repo, number) -> dict | None` (existing), `review_queue.issue_snippet(title, body)` (existing), the existing `on_decide(proposal, verdict)` closure (writes the decision, clears the drawer for that proposal, refreshes the queue).
- Produces: app-level inputs `drawer_approve`, `drawer_reject`, `drawer_skip` (plain `ui.input_action_button`s — the drawer is app-level, not a module, so no namespacing).

No unit tests — app wiring is manually verified (Global Constraints).

- [ ] **Step 1: Render params in words and the sibling block in `_drawer_proposal`**

Replace `_drawer_proposal` in `src/triage_verse/review_app/app.py`:

```python
def _close_duplicate_params(params: dict) -> str:
    canonical = params.get("canonical")
    bits = [f"canonical: {canonical or '(not stated)'}"]
    if params.get("cross_repo_option"):
        bits.append(f"cross-repo: {params['cross_repo_option']}")
    return " · ".join(bits)


def _drawer_sibling(proposal: dict) -> list:
    parts = [ui.h4("Duplicate sibling")]
    sibling = review_queue.duplicate_sibling(proposal)
    if sibling is None:
        parts.append(ui.p("(sibling not identified from evidence)"))
        return parts
    repo, number = sibling
    github_url = f"https://github.com/{repo}/issues/{number}"
    item = drawer.load_item(_con, repo, number)
    if item is None:
        parts += [
            ui.p(f"{repo}#{number} — (not found in mirror)"),
            ui.p(ui.a("Open on GitHub ↗", href=github_url, target="_blank")),
        ]
        return parts
    parts += [
        ui.h5(f"{repo}#{number}: {item['title']}"),
        ui.p(_drawer_meta_line(item), class_="drawer-meta"),
        ui.div(*[ui.span(label, class_="drawer-label") for label in item["labels"]]),
        ui.pre(review_queue.issue_snippet(item["title"], item["body"])),
        ui.p(ui.a("Open on GitHub ↗", href=item["github_url"], target="_blank")),
    ]
    return parts


def _drawer_proposal(proposal: dict) -> list:
    if proposal["action"] == "close-duplicate":
        params_line = _close_duplicate_params(proposal["params"])
    else:
        params_line = str(proposal["params"])
    parts = [
        ui.h4("Proposal"),
        ui.p(f"{proposal['action']}: {params_line}"),
        ui.p(f"confidence: {proposal.get('confidence', 0.0):.2f}"),
        ui.p(proposal.get("rationale") or "(no rationale)"),
        ui.div(
            ui.input_action_button(
                "drawer_approve",
                "Approve",
                style="background-color: #2e7d32; color: white;",
            ),
            ui.input_action_button(
                "drawer_reject",
                "Reject",
                style="background-color: #c62828; color: white;",
            ),
            ui.input_action_button(
                "drawer_skip", "Skip", style="background-color: #757575; color: white;"
            ),
            style="display: flex; gap: 0.5rem; margin-bottom: 0.75rem;",
        ),
    ]
    if proposal["action"] == "close-duplicate":
        parts += _drawer_sibling(proposal)
    parts += [
        ui.h4("Linked evidence"),
        ui.tags.ul(
            *[
                ui.tags.li(ui.a(url, href=url, target="_blank"))
                for url in proposal.get("evidence", [])
            ]
        ),
    ]
    return parts
```

Note: `_drawer_proposal` and `_drawer_sibling` read module-level `_con` — same pattern as the existing `_row_snippet`.

- [ ] **Step 2: Wire the drawer decide effects in `server`**

Add next to the existing `_drawer_close` effect:

```python
    def _decide_from_drawer(verdict: str) -> None:
        state = drawer_state.get()
        if state is not None:
            on_decide(state["proposal"], verdict)

    @reactive.effect
    @reactive.event(input.drawer_approve)
    def _drawer_approve():
        _decide_from_drawer("approved")

    @reactive.effect
    @reactive.event(input.drawer_reject)
    def _drawer_reject():
        _decide_from_drawer("rejected")

    @reactive.effect
    @reactive.event(input.drawer_skip)
    def _drawer_skip():
        _decide_from_drawer("skipped")
```

(`on_decide` already clears `drawer_state` when the decided proposal is the drawer's, so the drawer closes itself.)

- [ ] **Step 3: Smoke-run**

Run: `uv run pytest && uv run python -c "import triage_verse.review_app.app"`
Expected: tests PASS; import raises nothing.

- [ ] **Step 4: Commit**

```bash
git add src/triage_verse/review_app/app.py
git commit -m "feat(review-app): drawer decide buttons and duplicate-sibling evidence"
```

---

### Task 5: End-to-end manual verification and CI gates

**Files:**
- None (verification only; fix anything found where it lives).

**Interfaces:**
- Consumes: everything above; the `verify` project skill (build/launch/drive recipe for the review app).

- [ ] **Step 1: Run all CI gates locally**

Run: `make py-check-format && make py-check-types && make py-check-tests`
Expected: all three PASS. If `py-check-format` fails, run `make py-fix` (ruff) and re-run.

- [ ] **Step 2: Drive the app against fixture data (use the `verify` skill's recipe)**

Seed a temp mirror + proposals dir containing at least: one `add-label` proposal, one `close` proposal (open issue in mirror), and one `close-duplicate` proposal whose two evidence URLs both resolve to mirrored open issues. Launch with:

```bash
TRIAGE_VERSE_DB=/tmp/3d/mirror.sqlite \
TRIAGE_VERSE_PROPOSALS=/tmp/3d/proposals \
TRIAGE_VERSE_DECISIONS=/tmp/3d/decisions \
uv run shiny run src/triage_verse/review_app/app.py
```

Verify, per the spec's definition of done:
1. Close-action rows show the red action badge and only Review evidence / Skip.
2. Review evidence opens the drawer; Approve there writes a decision record (check `/tmp/3d/decisions/**.jsonl`) and removes the row.
3. The close-duplicate drawer shows the sibling's title/state/snippet and GitHub link; canonical / cross-repo render in words.
4. "Approve visible label/priority rows" decides only the `add-label` row; close rows remain.
5. Low-stakes rows still decide from row buttons, and the drawer's buttons work for them too.

- [ ] **Step 3: Commit any fixes, then hand off to PR creation**

```bash
git add -A && git commit -m "fix(review-app): <whatever the drive-through surfaced>"  # only if needed
```
