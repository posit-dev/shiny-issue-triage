# Plan 3e: Keyboard-Driven Review + Edit Action Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add keyboard navigation (`j`/`k`/`a`/`r`/`s`/`e`/`o`/`Enter`/`Escape`) and an edit-before-approve action to the review-queue Shiny app, per `docs/superpowers/specs/2026-07-09-plan-3e-keyboard-review-design.md` (issue #35).

**Architecture:** All new logic that can be pure lives in the already-tested modules — `decisions.record` grows a `params` override, `review_queue` gains the key→action map and selection clamping, `dashboard.category_precision` learns the `edited` verdict. `review_app/app.py` stays a thin wire-up: a JS keydown forwarder, a `selected` reactive index, and a `ui.modal` edit dialog.

**Tech Stack:** Python 3.12, Shiny for Python (server-rendered, no React), pytest, uv.

## Global Constraints

- Plain server-rendered Shiny for Python only — no shinyreact/React (Plan 3a decision).
- 1:1 module↔test convention: `src/triage_verse/<name>.py` ↔ `tests/triage_verse/test_<name>.py`.
- `review_queue.py`, `decisions.py`, `dashboard.py` must not import Shiny.
- Decision records: `params` is always "the thing the executor should apply"; `proposed_params` appears only on `edited` records.
- Precision: `judged = approved + rejected + edited`; `precision = approved / judged`.
- Checks: `make py-check` (ruff format, ruff check, mypy/pyright as wired, pytest) must pass before the PR.

---

### Task 1: `decisions.record` params override

**Files:**
- Modify: `src/triage_verse/decisions.py`
- Test: `tests/triage_verse/test_decisions.py`

**Interfaces:**
- Produces: `decisions.record(proposal: dict, verdict: str, *, params: dict | None = None) -> dict`. When `params` is given, the returned record's `params` is the override and `proposed_params` holds `proposal["params"]`; when omitted, behavior is unchanged and `proposed_params` is absent.

- [ ] **Step 1: Write the failing tests** — append to `tests/triage_verse/test_decisions.py`:

```python
def test_record_edited_params_override():
    rec = decisions.record(
        _proposal(), "edited", params={"label": "good first issue"}
    )
    assert rec["verdict"] == "edited"
    assert rec["params"] == {"label": "good first issue"}
    assert rec["proposed_params"] == {"label": "bug"}


def test_record_without_override_has_no_proposed_params():
    rec = decisions.record(_proposal(), "approved")
    assert "proposed_params" not in rec
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_decisions.py -v`
Expected: `test_record_edited_params_override` FAILS with `TypeError: record() got an unexpected keyword argument 'params'`; the other new test passes (guards the current shape).

- [ ] **Step 3: Implement** — in `src/triage_verse/decisions.py`, replace the `record` function with:

```python
def record(proposal: dict, verdict: str, *, params: dict | None = None) -> dict:
    rec = {
        "id": uuid.uuid4().hex,
        "proposal_id": proposal["id"],
        "repo": proposal["repo"],
        "issue": proposal["issue"],
        "action": proposal["action"],
        "params": proposal["params"] if params is None else params,
        "verdict": verdict,
        "confidence": proposal.get("confidence"),
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if params is not None:
        rec["proposed_params"] = proposal["params"]
    return rec
```

Also update the module docstring's first line to: `"""Record human review decisions (approve/reject/skip/edit) on proposals into a JSONL log."""`

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_decisions.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/decisions.py tests/triage_verse/test_decisions.py
git commit -m "feat(decisions): params override for edited verdicts"
```

---

### Task 2: `review_queue` key map + selection clamping

**Files:**
- Modify: `src/triage_verse/review_queue.py`
- Test: `tests/triage_verse/test_review_queue.py`

**Interfaces:**
- Produces: `review_queue.KEY_ACTIONS: dict[str, str]` mapping browser `KeyboardEvent.key` values to action names (`next`, `prev`, `approve`, `reject`, `skip`, `edit`, `open`, `close`); `review_queue.clamp_index(index: int | None, length: int) -> int | None` (None for empty queues, 0 when index is None, otherwise clamped to `[0, length-1]`).

- [ ] **Step 1: Write the failing tests** — append to `tests/triage_verse/test_review_queue.py`:

```python
def test_key_actions_cover_documented_bindings():
    assert review_queue.KEY_ACTIONS == {
        "j": "next",
        "k": "prev",
        "a": "approve",
        "r": "reject",
        "s": "skip",
        "e": "edit",
        "o": "open",
        "Enter": "open",
        "Escape": "close",
    }


def test_clamp_index():
    assert review_queue.clamp_index(None, 0) is None
    assert review_queue.clamp_index(3, 0) is None
    assert review_queue.clamp_index(None, 5) == 0
    assert review_queue.clamp_index(-1, 5) == 0
    assert review_queue.clamp_index(2, 5) == 2
    assert review_queue.clamp_index(7, 5) == 4  # queue shrank under selection
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -v`
Expected: both new tests FAIL with `AttributeError` on `KEY_ACTIONS` / `clamp_index`.

- [ ] **Step 3: Implement** — append to `src/triage_verse/review_queue.py` (after `SUPPORTED_ACTIONS`):

```python
# Browser KeyboardEvent.key -> review action. Mirrored into the app's JS
# keydown listener via json.dumps so the binding lives in exactly one place.
KEY_ACTIONS = {
    "j": "next",
    "k": "prev",
    "a": "approve",
    "r": "reject",
    "s": "skip",
    "e": "edit",
    "o": "open",
    "Enter": "open",
    "Escape": "close",
}


def clamp_index(index: int | None, length: int) -> int | None:
    """Clamp a selection index to a queue of `length`; None means no selection."""
    if length <= 0:
        return None
    if index is None:
        return 0
    return max(0, min(index, length - 1))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/review_queue.py tests/triage_verse/test_review_queue.py
git commit -m "feat(review-queue): key-action map and selection clamping"
```

---

### Task 3: `edited` verdict in per-category precision

**Files:**
- Modify: `src/triage_verse/dashboard.py:66-89` (`category_precision`)
- Modify: `src/triage_verse/review_app/app.py` (`precision_ui` columns)
- Test: `tests/triage_verse/test_dashboard.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `dashboard.category_precision` rows gain an `"edited": int` key; `judged = approved + rejected + edited`; `precision = approved / judged`.

- [ ] **Step 1: Update the existing test and add an edited case** — in `tests/triage_verse/test_dashboard.py`, replace `test_category_precision_per_action` with:

```python
def test_category_precision_per_action(tmp_path):
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        decisions_dir / "2026" / "W01.jsonl",
        [
            {"action": "add-label", "verdict": "approved"},
            {"action": "add-label", "verdict": "approved"},
            {"action": "add-label", "verdict": "rejected"},
            {"action": "add-label", "verdict": "edited"},
            {"action": "add-label", "verdict": "skipped"},
            {"action": "set-priority", "verdict": "skipped"},
            {"verdict": "approved"},  # missing action: skipped
        ],
    )
    assert dashboard.category_precision(decisions_dir) == [
        {
            "action": "add-label",
            "approved": 2,
            "edited": 1,
            "rejected": 1,
            "skipped": 1,
            "precision": 2 / 4,
        },
        {
            "action": "set-priority",
            "approved": 0,
            "edited": 0,
            "rejected": 0,
            "skipped": 1,
            "precision": None,
        },
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_dashboard.py::test_category_precision_per_action -v`
Expected: FAIL (missing `edited` key, wrong precision).

- [ ] **Step 3: Implement** — in `src/triage_verse/dashboard.py`, update `category_precision`'s docstring second line to `Skips are shown but excluded from the rate — a skip is "not judged", not "wrong". Edits are judged-but-not-approved-as-proposed.` and replace the loop body:

```python
    for action in sorted(by_action):
        verdicts = by_action[action]
        judged = verdicts["approved"] + verdicts["rejected"] + verdicts["edited"]
        out.append(
            {
                "action": action,
                "approved": verdicts["approved"],
                "edited": verdicts["edited"],
                "rejected": verdicts["rejected"],
                "skipped": verdicts["skipped"],
                "precision": verdicts["approved"] / judged if judged else None,
            }
        )
```

Then in `src/triage_verse/review_app/app.py`, update `precision_ui` to include the new column:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_dashboard.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/dashboard.py src/triage_verse/review_app/app.py tests/triage_verse/test_dashboard.py
git commit -m "feat(dashboard): count edited verdicts in category precision"
```

---

### Task 4: App wiring — keyboard, selection highlight, edit modal

**Files:**
- Modify: `src/triage_verse/review_app/app.py`

**Interfaces:**
- Consumes: `review_queue.KEY_ACTIONS`, `review_queue.clamp_index` (Task 2); `decisions.record(..., params=...)` (Task 1).
- Produces: nothing consumed by later tasks.

No unit tests (thin Shiny wiring, consistent with 3a–3c); verified end-to-end in Task 5. Steps below give the complete code.

- [ ] **Step 1: Add the JS keydown forwarder and selection CSS.** In `app.py`, add `import json` to the imports, then below `_DRAWER_CSS` add:

```python
_KB_CSS = """
.kb-selected > .card { outline: 3px solid #0969da; outline-offset: -1px; }
"""

_KB_JS = """
document.addEventListener("keydown", (e) => {
  const actions = %s;
  if (!(e.key in actions)) return;
  if (e.target.closest("input, textarea, select, [contenteditable='true']")) return;
  if (document.querySelector(".modal.show")) return;
  e.preventDefault();
  Shiny.setInputValue("key_action", actions[e.key], {priority: "event"});
});
""" % json.dumps(review_queue.KEY_ACTIONS)
```

and change `app_ui`'s header to include both, plus a key legend at the top of the Queue tab:

```python
app_ui = ui.page_navbar(
    ui.nav_panel(
        "Queue",
        ui.p(
            "Keys: j/k select · a approve · r reject · s skip · e edit"
            " · o/Enter open · Esc close",
            class_="text-muted",
        ),
        ui.input_action_button("approve_visible", "Approve visible rows"),
        ui.output_ui("queue_ui"),
        ui.output_ui("drawer_ui"),
    ),
    dashboard_panel,
    title="Triage review",
    header=[ui.tags.style(_DRAWER_CSS + _KB_CSS), ui.tags.script(_KB_JS)],
)
```

- [ ] **Step 2: Add an Edit button to the row module.** In `row_ui`, insert between the Reject and Skip buttons:

```python
            ui.input_action_button(
                "edit", "Edit", style="background-color: #f9a825; color: black;"
            ),
```

In `row_server`, add an `on_edit: Callable[[dict], None]` parameter (after `on_decide`) and:

```python
    @reactive.effect
    @reactive.event(input.edit)
    def _edit():
        on_edit(proposal)
```

- [ ] **Step 3: Wire selection, key handling, and the edit modal in `server`.** After `drawer_state = ...` add:

```python
    selected = reactive.value[int | None](None)
    edit_target = reactive.value[dict | None](None)
```

Generalize `on_decide` to carry edited params:

```python
    def on_decide(proposal: dict, verdict: str, params: dict | None = None) -> None:
        decisions.write(
            [decisions.record(proposal, verdict, params=params)], DECISIONS_DIR
        )
        state = drawer_state.get()
        if state is not None and state["proposal"]["id"] == proposal["id"]:
            drawer_state.set(None)
        refresh()
```

Add the edit-modal open/save handlers (after `on_open`):

```python
    def on_edit(proposal: dict) -> None:
        edit_target.set(proposal)
        ui.modal_show(
            ui.modal(
                ui.p(_row_label(proposal)),
                *[
                    ui.input_text(f"edit_{key}", key, value=str(value))
                    for key, value in proposal["params"].items()
                ],
                title="Edit proposal",
                footer=[
                    ui.input_action_button(
                        "edit_save", "Approve edited", class_="btn btn-success"
                    ),
                    ui.modal_button("Cancel"),
                ],
                easy_close=True,
            )
        )

    @reactive.effect
    @reactive.event(input.edit_save)
    def _edit_save():
        proposal = edit_target.get()
        if proposal is None:
            return
        params = {key: input[f"edit_{key}"]().strip() for key in proposal["params"]}
        if any(not v for v in params.values()):
            return  # keep the modal open until every field has a value
        edit_target.set(None)
        ui.modal_remove()
        on_decide(proposal, "edited", params=params)
```

Add the key dispatcher:

```python
    @reactive.effect
    @reactive.event(input.key_action)
    def _key_action():
        action = input.key_action()
        if action == "close":
            drawer_state.set(None)
            return
        rows = queue.get()
        sel = review_queue.clamp_index(selected.get(), len(rows))
        if sel is None:
            return
        if action == "next":
            selected.set(review_queue.clamp_index(sel + 1, len(rows)))
        elif action == "prev":
            selected.set(review_queue.clamp_index(sel - 1, len(rows)))
        elif action == "approve":
            on_decide(rows[sel], "approved")
        elif action == "reject":
            on_decide(rows[sel], "rejected")
        elif action == "skip":
            on_decide(rows[sel], "skipped")
        elif action == "edit":
            on_edit(rows[sel])
        elif action == "open":
            on_open(rows[sel])
```

- [ ] **Step 4: Render the selection highlight.** Replace `queue_ui` with:

```python
    @render.ui
    def queue_ui():
        rows = queue.get()
        if not rows:
            return ui.p("Queue empty — nothing to review.")
        sel = review_queue.clamp_index(selected.get(), len(rows))
        cards = []
        for i, proposal in enumerate(rows):
            row_id = proposal["id"]
            if row_id not in wired:
                row_server(
                    row_id,
                    proposal=proposal,
                    on_decide=on_decide,
                    on_open=on_open,
                    on_edit=on_edit,
                )
                wired.add(row_id)
            cards.append(
                ui.div(
                    row_ui(row_id, proposal, _row_snippet(proposal)),
                    class_="kb-selected" if i == sel else None,
                )
            )
        cards.append(
            ui.tags.script(
                "document.querySelector('.kb-selected')"
                "?.scrollIntoView({block: 'nearest'});"
            )
        )
        return ui.div(*cards)
```

- [ ] **Step 5: Run lint, format, and full tests**

Run: `make py-check`
Expected: PASS (format, lint, types, tests).

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/review_app/app.py
git commit -m "feat(review-app): keyboard-driven review and edit modal (Plan 3e)"
```

---

### Task 5: End-to-end verification + PR

**Files:**
- None (verification and PR only).

- [ ] **Step 1: Verify at the runtime surface** using the repo's `verify` recipe (launch the review app against fixture/real `.data/`, exercise: j/k selection highlight + scroll, a/r/s decide the highlighted row, e opens the pre-filled modal and "Approve edited" writes an `edited` record with `proposed_params`, o/Enter opens the drawer, Esc closes it, typing in the modal does not trigger verdicts, Dashboard precision table shows the `edited` column).

- [ ] **Step 2: Confirm a written edited record** — after the edit in Step 1:

Run: `grep -h edited .data/decisions/*/*.jsonl | tail -1 | uv run python -m json.tool`
Expected: a record with `"verdict": "edited"`, edited `params`, and `proposed_params` carrying the original.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin HEAD
gh pr create --base main --title "feat(review-app): keyboard-driven review + edit action (Plan 3e)" --body "Closes #35. See docs/superpowers/specs/2026-07-09-plan-3e-keyboard-review-design.md"
```
