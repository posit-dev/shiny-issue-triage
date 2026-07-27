# Decision Attribution (`decided_by` + `reason`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stamp every human review decision with who decided it (`decided_by`) and, on reject/edit, why (`reason`), with no data migration.

**Architecture:** `decisions.record()` gains a required `decided_by` and an optional `reason`; a new cached `decisions.current_actor()` resolves the local reviewer's GitHub login. The Shiny review app wires the actor into all human decision paths, adds a reject-reason modal (mirroring the existing edit modal) across the three reject entry points, and adds a rationale-seeded reason field to the edit modal. Automated `execute --auto` is untouched (stays `decided_by="autonomy"`).

**Tech Stack:** Python 3.14, uv, Shiny for Python, pytest, ruff, pyright. GitHub access via `gh` CLI funneled through the `triage_verse.gh` egress guard.

## Global Constraints

- **No migration.** JSONL is append-only; all readers access decision fields with `dict.get`. Old records missing `decided_by`/`reason` stay valid — never rewrite history.
- **`reason` is written only when non-empty.** Empty/None reason is omitted from the record entirely.
- **Automated attribution is out of scope.** Do not touch `execute --auto` (`executor.py:368`, `decided_by="autonomy"`) or `autonomy.py`'s precision filter (`decided_by != "autonomy"`).
- **All GitHub calls go through `triage_verse.gh.run_gh`.** `gh api user --jq .login` classifies as a REST read and passes the guard; never shell out to `gh` directly.
- **Primary gate:** `make py-check` (ruff format check + lint, pyright, pytest) must pass before any task is considered done.
- Run a single test with `uv run pytest tests/triage_verse/test_foo.py::test_bar`.

---

### Task 1: `decisions.record()` — add `decided_by` (required) + `reason` (optional)

**Files:**
- Modify: `src/triage_verse/decisions.py:12-26`
- Test: `tests/triage_verse/test_decisions.py`

**Interfaces:**
- Produces: `decisions.record(proposal: dict, verdict: str, *, params: dict | None = None, decided_by: str, reason: str | None = None) -> dict`. Always sets `rec["decided_by"] = decided_by`. Sets `rec["reason"] = reason` only when `reason` is a non-empty string.

- [ ] **Step 1: Update the existing tests to pass `decided_by` and add new assertions**

The existing tests in `tests/triage_verse/test_decisions.py` call `record()` without `decided_by`; they must pass it now. Update every `decisions.record(...)` call in that file to include `decided_by="alice"`, and add these two new tests:

```python
def test_record_stamps_decided_by():
    rec = decisions.record(_proposal(), "approved", decided_by="alice")
    assert rec["decided_by"] == "alice"


def test_record_omits_empty_reason_includes_nonempty():
    no_reason = decisions.record(_proposal(), "approved", decided_by="alice")
    assert "reason" not in no_reason

    blank = decisions.record(_proposal(), "approved", decided_by="alice", reason="")
    assert "reason" not in blank

    with_reason = decisions.record(
        _proposal(), "rejected", decided_by="alice", reason="wrong label"
    )
    assert with_reason["reason"] == "wrong label"
```

Also update the three existing tests (`test_record_copies_proposal_fields`, `test_record_edited_params_override`, `test_record_without_override_has_no_proposed_params`) and `test_write_appends_weekly_partition` so each `record(...)` call passes `decided_by="alice"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_decisions.py -v`
Expected: FAIL — `record()` raises `TypeError: missing required keyword-only argument 'decided_by'` (once the signature is not yet updated) or the new assertions fail.

- [ ] **Step 3: Update `record()` implementation**

In `src/triage_verse/decisions.py`, change `record` to:

```python
def record(
    proposal: dict,
    verdict: str,
    *,
    params: dict | None = None,
    decided_by: str,
    reason: str | None = None,
) -> dict:
    rec = {
        "id": uuid.uuid4().hex,
        "proposal_id": proposal["id"],
        "repo": proposal["repo"],
        "issue": proposal["issue"],
        "action": proposal["action"],
        "params": proposal["params"] if params is None else params,
        "verdict": verdict,
        "confidence": proposal.get("confidence"),
        "decided_by": decided_by,
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if params is not None:
        rec["proposed_params"] = proposal["params"]
    if reason:
        rec["reason"] = reason
    return rec
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_decisions.py -v`
Expected: PASS (all, including the two new tests).

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/decisions.py tests/triage_verse/test_decisions.py
git commit -m "feat(decisions): record decided_by + optional reason (#40)"
```

---

### Task 2: `decisions.current_actor()` — cached reviewer-identity resolver

**Files:**
- Modify: `src/triage_verse/decisions.py` (add import of `functools`, `os`, and `gh`; add function)
- Test: `tests/triage_verse/test_decisions.py`

**Interfaces:**
- Produces: `decisions.current_actor() -> str`. Returns the local GitHub login via `gh api user --jq .login`; on any failure falls back to `$USER`, then `"unknown"`. Result is cached with `functools.cache` (one `gh.run_gh` call per process). Never raises.

- [ ] **Step 1: Write the failing tests**

Add to `tests/triage_verse/test_decisions.py`:

```python
import pytest

from triage_verse import gh


@pytest.fixture(autouse=True)
def _clear_actor_cache():
    decisions.current_actor.cache_clear()
    yield
    decisions.current_actor.cache_clear()


def test_current_actor_uses_gh_login(monkeypatch):
    calls = []

    def fake_run_gh(args, **kwargs):
        calls.append(args)
        return "octocat\n"

    monkeypatch.setattr(gh, "run_gh", fake_run_gh)
    assert decisions.current_actor() == "octocat"
    # cached: a second call does not invoke gh again
    assert decisions.current_actor() == "octocat"
    assert len(calls) == 1
    assert calls[0] == ["api", "user", "--jq", ".login"]


def test_current_actor_falls_back_to_user_env(monkeypatch):
    def boom(args, **kwargs):
        raise gh.GhError("not authenticated")

    monkeypatch.setattr(gh, "run_gh", boom)
    monkeypatch.setenv("USER", "barret")
    assert decisions.current_actor() == "barret"


def test_current_actor_falls_back_to_unknown(monkeypatch):
    monkeypatch.setattr(gh, "run_gh", lambda args, **kwargs: "")
    monkeypatch.delenv("USER", raising=False)
    assert decisions.current_actor() == "unknown"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_decisions.py -k current_actor -v`
Expected: FAIL — `AttributeError: module 'triage_verse.decisions' has no attribute 'current_actor'`.

- [ ] **Step 3: Implement `current_actor()`**

In `src/triage_verse/decisions.py`, add to the imports:

```python
import functools
import os

from . import gh, jsonl_log
```

(replace the existing `from . import jsonl_log`), and add:

```python
@functools.cache
def current_actor() -> str:
    """Resolve the local reviewer's identity for decision attribution.

    Tries the GitHub login (a REST read that passes the egress guard), then
    `$USER`, then "unknown". Cached: resolved once per process. Never raises.
    """
    try:
        login = gh.run_gh(["api", "user", "--jq", ".login"], retries=1).strip()
        if login:
            return login
    except Exception:
        pass
    return os.environ.get("USER") or "unknown"
```

Note: `retries=1` avoids `run_gh`'s default 5-attempt/30s backoff hanging the review app when `gh` is unauthenticated.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_decisions.py -k current_actor -v`
Expected: PASS (all three).

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/decisions.py tests/triage_verse/test_decisions.py
git commit -m "feat(decisions): current_actor() resolves reviewer identity (#40)"
```

---

### Task 3: Wire `decided_by` (+ `reason` plumbing) into human decision paths

**Files:**
- Modify: `src/triage_verse/review_app/app.py:258-277` (`app_audit_reject`)
- Modify: `src/triage_verse/review_app/app.py:563-571` (`on_decide`)
- Modify: `src/triage_verse/review_app/app.py:787-799` (`approve_visible`)
- Test: `tests/triage_verse/test_review_app_audit.py`

**Interfaces:**
- Consumes: `decisions.record(..., decided_by=..., reason=...)` (Task 1), `decisions.current_actor()` (Task 2).
- Produces: `on_decide(proposal: dict, verdict: str, params: dict | None = None, reason: str | None = None) -> None` — internal to the server; stamps `decided_by=decisions.current_actor()` and passes `reason` through to `decisions.record`.

- [ ] **Step 1: Update the audit-reject test to assert a resolved actor**

In `tests/triage_verse/test_review_app_audit.py`, extend `test_app_audit_reject_records_rejected_decision` to monkeypatch the actor and assert it is written. Add `monkeypatch` to the signature and, before calling `app_audit_reject`:

```python
    from triage_verse import decisions

    monkeypatch.setattr(decisions, "current_actor", lambda: "barret")
```

Then after reading the record back, add:

```python
    assert rec["decided_by"] == "barret"
```

Keep the existing `assert rec.get("decided_by") != "autonomy"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_review_app_audit.py::test_app_audit_reject_records_rejected_decision -v`
Expected: FAIL — `decided_by` is the hardcoded `"human"`, not `"barret"`.

- [ ] **Step 3: Wire the actor into the three human paths**

In `src/triage_verse/review_app/app.py`:

`app_audit_reject` — replace the hardcoded actor:

```python
        "verdict": "rejected",
        "decided_by": decisions.current_actor(),
```

`on_decide` — add the `reason` parameter and stamp the actor:

```python
    def on_decide(
        proposal: dict, verdict: str, params: dict | None = None, reason: str | None = None
    ) -> None:
        _select(proposal)
        decisions.write(
            [
                decisions.record(
                    proposal,
                    verdict,
                    params=params,
                    decided_by=decisions.current_actor(),
                    reason=reason,
                )
            ],
            DECISIONS_DIR,
        )
        state = drawer_state.get()
        if state is not None and state["proposal"]["id"] == proposal["id"]:
            drawer_state.set(None)
        refresh()
```

`approve_visible` — stamp the actor on each bulk approval (reason stays empty):

```python
        decisions.write(
            [
                decisions.record(p, "approved", decided_by=decisions.current_actor())
                for p in queue.get()
                if p["action"] not in review_queue.HIGH_STAKES_ACTIONS
            ],
            DECISIONS_DIR,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/triage_verse/test_review_app_audit.py -v`
Expected: PASS.

- [ ] **Step 5: Type/lint gate**

Run: `make py-check`
Expected: PASS (ruff + pyright clean; whole pytest suite green).

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/review_app/app.py tests/triage_verse/test_review_app_audit.py
git commit -m "feat(review-app): stamp decided_by on human decisions (#40)"
```

---

### Task 4: Reject-reason modal across all three reject entry points

The main-Queue row reject (button + keyboard `r`) and the drawer reject currently call `on_decide(proposal, "rejected")` instantly. Route all three through a single reject modal (mirroring the existing edit modal at `app.py:586-602`) that captures an **optional** free-text reason. The reason field starts blank with placeholder "why was this wrong?"; the agent's `rationale` is shown read-only for context. Confirming with an empty reason is one click, so keyboard triage stays fast.

**Files:**
- Modify: `src/triage_verse/review_app/app.py` — server body (add `reject_target` reactive value, `_open_reject_modal`, `input.reject_save` handler; repoint `input.reject` in `row_server`, the `key_action == "reject"` branch, and `input.drawer_reject`).

**Interfaces:**
- Consumes: `on_decide(proposal, "rejected", reason=...)` (Task 3).
- Produces: `_open_reject_modal(proposal: dict) -> None` — opens the reject modal for `proposal` and stores it in `reject_target`.

**Testing note:** This task is Shiny reactive wiring; the repo's convention (see `test_review_app_audit.py`) tests extracted pure helpers, not reactive server effects. There is no new pure logic here — the reason-to-record plumbing is already covered by Task 1 and Task 3 tests. Verify via `make py-check` (types) plus the manual checklist in Step 4.

- [ ] **Step 1: Add the reject modal machinery**

In the server body of `app.py`, near `edit_target`/`on_edit`, add a `reject_target` reactive value and a modal opener that mirrors the edit modal:

```python
    reject_target = reactive.value[dict | None](None)

    def _open_reject_modal(proposal: dict) -> None:
        _select(proposal)
        reject_target.set(proposal)
        ui.modal_show(
            ui.modal(
                ui.p(_row_label(proposal)),
                ui.p(proposal.get("rationale") or "(no rationale)", class_="text-muted"),
                ui.input_text_area(
                    "reject_reason",
                    "Reason (optional)",
                    value="",
                    placeholder="why was this wrong?",
                    width="100%",
                ),
                title="Reject proposal",
                footer=[
                    ui.input_action_button(
                        "reject_save", "Confirm reject", class_="btn btn-danger"
                    ),
                    ui.modal_button("Cancel"),
                ],
                easy_close=True,
            )
        )

    @reactive.effect
    @reactive.event(input.reject_save)
    def _reject_save():
        proposal = reject_target.get()
        if proposal is None:
            return
        reason = input.reject_reason().strip() or None
        reject_target.set(None)
        ui.modal_remove()
        on_decide(proposal, "rejected", reason=reason)
```

- [ ] **Step 2: Repoint the three reject entry points to open the modal**

(a) **Drawer reject** — replace `_drawer_reject`'s body:

```python
    @reactive.effect
    @reactive.event(input.drawer_reject)
    def _drawer_reject():
        state = drawer_state.get()
        if state is not None:
            _open_reject_modal(state["proposal"])
```

(b) **Keyboard `r`** — in `_key_action`, the `action in ("approve", "reject")` branch, change the non-high-stakes else-branch so reject opens the modal instead of deciding directly:

```python
            else:
                if action == "approve":
                    on_decide(proposal, "approved")
                else:
                    _open_reject_modal(proposal)
```

(High-stakes reject already routes to `on_open`; leave that path unchanged. The global keydown handler ignores keys while a modal is open, so opening the modal safely pauses shortcuts.)

(c) **Row reject button** — `row_server` (a module) cannot see the top-level `_open_reject_modal`. Route the row's reject through the existing `on_decide` callback contract by having `row_server` call a new `on_reject` callback. Add `on_reject: Callable[[dict], None]` to `row_server`'s parameters, change `_reject` to call `on_reject(proposal)`, and pass `on_reject=_open_reject_modal` from `_render_cards` where `row_server(...)` is wired.

In `row_server` signature add `on_reject: Callable[[dict], None],` and change:

```python
    @reactive.effect
    @reactive.event(input.reject)
    def _reject():
        on_reject(proposal)
```

In `_render_cards`, update the `row_server(...)` call to include `on_reject=_open_reject_modal`.

- [ ] **Step 3: Type/lint gate**

Run: `make py-check`
Expected: PASS.

- [ ] **Step 4: Manual verification (review app)**

Launch the app against a mirror that has pending proposals (use the `verify` skill, or `shiny run src/triage_verse/review_app/app.py`). Confirm:
- Clicking **Reject** on a Queue row opens the modal; the agent rationale shows read-only; the reason box is blank with the placeholder.
- Confirming with an empty reason writes a rejected decision (no `reason` key); confirming with text writes `reason`.
- Pressing **`r`** on a selected non-high-stakes row opens the same modal; shortcuts are inert while it is open; Cancel leaves the proposal undecided.
- **Reject** inside the drawer opens the same modal.
Inspect the newest line in `.data/decisions/…jsonl` to confirm `decided_by` and (when typed) `reason`.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/review_app/app.py
git commit -m "feat(review-app): capture reason on every reject via modal (#40)"
```

---

### Task 5: Seed the edit modal's reason from the agent rationale

The edit modal (`app.py:586-602`) already collects edited params and approves as `"edited"`. Add a reason field pre-filled with the proposal's `rationale` (editing affirms the agent's reasoning, so its rationale is the natural "why"), and pass it through on save.

**Files:**
- Modify: `src/triage_verse/review_app/app.py` — `on_edit` (add the reason field to the modal), `_edit_save` (read and forward `reason`).

**Interfaces:**
- Consumes: `on_decide(proposal, "edited", params=..., reason=...)` (Task 3).

**Testing note:** Reactive wiring; the reason-to-record plumbing is covered by Task 1/Task 3 tests. Verify via `make py-check` and the Step 3 manual check.

- [ ] **Step 1: Add the reason field to the edit modal**

In `on_edit`, add a reason input after the per-param inputs, seeded with the rationale:

```python
        ui.modal_show(
            ui.modal(
                ui.p(_row_label(proposal)),
                *[
                    ui.input_text(f"edit_{key}", key, value=str(value))
                    for key, value in proposal["params"].items()
                ],
                ui.input_text_area(
                    "edit_reason",
                    "Reason",
                    value=proposal.get("rationale") or "",
                    width="100%",
                ),
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
```

- [ ] **Step 2: Forward the reason on save**

In `_edit_save`, read the reason and pass it through:

```python
    @reactive.effect
    @reactive.event(input.edit_save)
    def _edit_save():
        proposal = edit_target.get()
        if proposal is None:
            return
        params = {key: input[f"edit_{key}"]().strip() for key in proposal["params"]}
        if any(not v for v in params.values()):
            return  # keep the modal open until every field has a value
        reason = input.edit_reason().strip() or None
        edit_target.set(None)
        ui.modal_remove()
        on_decide(proposal, "edited", params=params, reason=reason)
```

- [ ] **Step 3: Type/lint gate + manual check**

Run: `make py-check`
Expected: PASS.

Manual (review app): open **Edit** on a proposal that has a rationale; confirm the Reason box is pre-filled with that rationale and is editable; save and confirm the decision record's `reason` reflects the (possibly edited) text.

- [ ] **Step 4: Commit**

```bash
git add src/triage_verse/review_app/app.py
git commit -m "feat(review-app): seed edit-modal reason from agent rationale (#40)"
```

---

### Task 6: Full-suite gate + end-to-end verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full CI-equivalent gate**

Run: `make py-check`
Expected: PASS — ruff format check, ruff lint, pyright, and the entire pytest suite all green.

- [ ] **Step 2: Confirm no regression in autonomy metrics**

Run: `uv run pytest tests/triage_verse/test_autonomy.py tests/triage_verse/test_cli_autonomy.py -v`
Expected: PASS — human decisions now carrying a login string are still `!= "autonomy"`, so precision accounting is unchanged.

- [ ] **Step 3: End-to-end review-app verification**

Use the `verify` skill to launch the review app against a mirror with pending proposals and exercise: approve (row + bulk), reject with and without a reason (row, keyboard `r`, drawer), and edit (rationale-seeded reason). Inspect the resulting `.data/decisions/…jsonl` lines and confirm every human record has `decided_by` set to your GitHub login and `reason` present only where you typed one.

- [ ] **Step 4: Final commit (if any verification-driven fixes were needed)**

```bash
git add -A
git commit -m "test: verify decision attribution end-to-end (#40)"
```

---

## Self-Review

**Spec coverage:**
- Record schema (`decided_by` required, `reason` when non-empty, no migration) → Task 1. ✓
- `current_actor()` (`gh api user` → `$USER` → `"unknown"`, cached, never raises) → Task 2. ✓
- Wire `on_decide` / `approve_visible` / `app_audit_reject` → Task 3. ✓
- Reason on every reject; rationale as read-only context; blank on reject → Task 4. ✓
- Edit reason pre-filled from rationale → Task 5. ✓
- Bulk approve leaves reason empty → Task 3 (`approve_visible` passes no `reason`). ✓
- `execute --auto` stays `"autonomy"`; `autonomy.py` unchanged → Global Constraints + Task 6 Step 2 regression check. ✓

**Deviations from the spec, recorded here (not by editing the spec):**
- The spec described the reject reason as an *inline* field; this plan uses a *modal* (Task 4), because the edit interaction is already a modal, a modal unifies the three reject entry points (row button, keyboard, drawer), and it is compatible with the global keydown handler (which suspends shortcuts while a modal is open). Same captured data, better consistency.
- The spec's "Approve/edit (drawer): reason pre-filled" is realized as: **edit** modal seeds reason from rationale (Task 5); **approve** carries no reason and stays a single instant action (the issue explicitly says "approvals can leave it empty"). Verdict-aware seeding is thus achieved through the point-of-verdict modals (reject blank, edit seeded) rather than a shared drawer field, which could not be blank-for-reject and seeded-for-approve simultaneously.

**Placeholder scan:** none — every code step has concrete code.

**Type consistency:** `current_actor() -> str`, `record(..., decided_by, reason=None)`, `on_decide(proposal, verdict, params=None, reason=None)`, `_open_reject_modal(proposal)`, and `row_server(..., on_reject)` are used consistently across Tasks 1–5.
