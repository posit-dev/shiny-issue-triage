# Plan 3e — Keyboard-driven review + edit action design

- **Date:** 2026-07-09
- **Owner:** Barret Schloerke
- **Status:** Design for review, pre-implementation
- **Builds on:** the Plan 3a review queue, Plan 3b drawer, and Plan 3c dashboard (`src/triage_verse/review_app/app.py`); the program design `docs/superpowers/specs/2026-06-12-shinyverse-issue-triage-design.md`
- **Issue:** #35 (Plan 3e: Keyboard-driven review + edit action), child of #9

This document stands alone: it restates the context it needs rather than pointing at section numbers in other files.

## 1. Goal and scope

The review app today is mouse-only: each queue row has Approve / Reject / Skip buttons, a title link that opens the evidence drawer, and a bulk "Approve visible rows" button. The program design targets ~50 decisions in ~10 minutes, which the master spec ties to keyboard-driven review; clicking three buttons per card doesn't get there. This slice adds:

**In scope**

- **Keyboard navigation and verdicts on the queue tab.** One row is always "selected" (visually highlighted). Keys:
  - `j` / `k` — move selection down / up (clamped at the ends, no wrap)
  - `a` / `r` / `s` — approve / reject / skip the selected row
  - `e` — open the edit dialog for the selected row
  - `o` or `Enter` — open the evidence drawer for the selected row
  - `Escape` — close the drawer (or the edit dialog)
- **An edit action** (keyboard `e` and a per-row **Edit** button): a modal pre-filled with the proposal's params (e.g. the label for `add-label`, the priority for `set-priority`). Saving writes a decision with verdict `edited`, the corrected params, and the original params preserved.
- **Dashboard follow-through:** the per-category precision table gains an `edited` column, and edited decisions count as "judged but not approved as proposed" in the precision rate.
- A one-line key legend at the top of the Queue tab so the bindings are discoverable.

**Out of scope (later slices)**

- `close` / `close-duplicate` proposals (#34, Plan 3d).
- Any React/shinyreact rewrite — this stays plain server-rendered Shiny, consistent with the 3a decision.
- Editing anything other than a proposal's `params` values (repo, issue, action type are fixed).
- Making the executor (Plan 4) aware of `edited` records — but the record shape is chosen so the executor can treat `edited` like `approved` with different params.

**Definition of done**

1. With the app running against real `.data/`, a full review pass — navigate, approve, reject, skip, edit, open/close drawer — is possible without touching the mouse.
2. Editing a proposal writes a durable decision record carrying both the corrected and the original params, and the proposal never reappears in the queue.
3. All new logic outside the Shiny wiring (key→action mapping, selection clamping, decision-record shape, precision math) has offline unit coverage.

## 2. Decision record for edits

`decisions.record(proposal, verdict)` grows an optional `params` override:

```
decisions.record(proposal, "edited", params={"label": "good first issue"})
```

produces the existing record shape plus:

```
{ ...,
  verdict: "edited",
  params: {"label": "good first issue"},   # the corrected value — what the executor should apply
  proposed_params: {"label": "bug"} }       # the model's original proposal, for precision analysis
```

`proposed_params` is only present on edited records. `params` stays "the thing to apply" for every verdict, so Plan 4's executor reads one field regardless of verdict. All verdicts still filter the proposal out of the queue via the existing single `proposal_id` rule.

## 3. Precision accounting

`dashboard.category_precision` currently computes `precision = approved / (approved + rejected)`, with skips excluded ("not judged"). An edit is a judgment: the model's exact proposal was wrong, but salvageable. So:

- new `edited` count column per action;
- `judged = approved + rejected + edited`;
- `precision = approved / judged` — an edited proposal does **not** count as approved, because graduated autonomy should key off "the model proposed exactly the right action", not "a human could fix it up".

## 4. Keyboard mechanics

A small JS keydown listener (inline `<script>` in the app header) forwards relevant keys to the server as a Shiny input event:

- Ignores keystrokes when focus is in an input/textarea/select or when a Bootstrap modal is open (so typing in the edit dialog never triggers verdicts).
- Sends `Shiny.setInputValue("key_action", <action>, {priority: "event"})` where `<action>` comes from a key→action map mirrored in Python (`review_queue.KEY_ACTIONS`) so the mapping is unit-testable and documented in one place each.

Server side, a `selected` reactive index into the current queue drives:

- a `kb-selected` CSS class on the selected card (outline highlight) plus a scroll-into-view nudge on re-render;
- `a`/`r`/`s`/`e`/`o` acting on `queue[selected]`;
- clamping via a pure helper `review_queue.clamp_index(index, length) -> int | None` — selection survives a decision by staying at the same position (which is now the next proposal), and returns `None` for an empty queue.

Re-rendering the whole card list on each selection move is accepted at current queue sizes (hundreds of rows), consistent with the existing render-everything approach; the JSONL-rescan scaling concern is already tracked separately in issue #29.

## 5. Edit dialog

`ui.modal` with one text input per key in the proposal's `params` dict (both supported actions have exactly one: `label` or `priority`), pre-filled with the current value, plus **Approve edited** / **Cancel** buttons. Saving calls `decisions.record(proposal, "edited", params=<new values>)` through the same write-then-refresh path as the other verdicts. Empty values are rejected client-side by simply not saving (the modal stays open). Free-text is deliberate — the config label vocabulary (`config/labels.yaml`) isn't wired into the app yet, and validating against it is a natural fast-follow once Plan 4 needs it.

## 6. Testing

- `review_queue.py`: `KEY_ACTIONS` completeness (every documented key maps to a known action) and `clamp_index` edge cases (empty queue, ends, after-shrink) in `test_review_queue.py`.
- `decisions.py`: `record(..., params=...)` sets `params`, preserves `proposed_params`, and leaves non-edited records unchanged, in `test_decisions.py`.
- `dashboard.py`: `category_precision` edited column and rate math in `test_dashboard.py`.
- `review_app/app.py`: remains manual/end-to-end verification (no Shiny test harness in the repo), driven via the repo's verify recipe.

## 7. Open items for your review

1. **Edited ≠ approved in the precision rate.** If you'd rather track "salvage rate" separately (edited as a soft approve), the counts are all in the log — only the rate formula would change.
2. **No label-vocabulary validation on edit.** Free text until Plan 4's executor defines what's applyable.
3. **Selection stays at the same index after a verdict** (i.e. jumps to the next card). If you'd rather it follow some other rule (e.g. stay on the same proposal id after `o`pen), say so.
