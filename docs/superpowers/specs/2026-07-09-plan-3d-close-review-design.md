# Plan 3d — Review close / close-duplicate proposals design

- **Date:** 2026-07-09
- **Owner:** Barret Schloerke
- **Status:** Design for review, pre-implementation
- **Builds on:** the Plan 3a review queue (`src/triage_verse/review_queue.py`, `src/triage_verse/review_app/app.py`), the Plan 3b drawer (`src/triage_verse/drawer.py`), and the Plan 2 proposals log (`src/triage_verse/proposals.py`)
- **Issue:** #34 (Plan 3d — Review close / close-duplicate proposals), child of #9 (Plan 3 — Review app)

This document stands alone: it restates the context it needs rather than pointing at section numbers in other files.

## 1. Goal and scope

The Plan 2 analysis pipeline already emits four proposal action types into the weekly proposals JSONL: `add-label`, `set-priority`, `close`, and `close-duplicate`. The review queue currently shows only the first two — `review_queue.SUPPORTED_ACTIONS` filters the rest out. That filter was deliberate: closing an issue is higher-stakes than adding a label, and judging a close from a 280-character body snippet is not acceptable. Now that the drawer (Plan 3b) renders the full issue — body, comment thread, labels, state — the evidence surface exists, and #34 asks to put `close` and `close-duplicate` proposals in front of the reviewer.

Proposal shapes, as produced by `proposals.build`:

- `close` — `params` is `{"reason": <duplicate|stale|not-planned|fixed|answered>}`, with the classifier's close-specific rationale and confidence; one evidence URL (the issue itself).
- `close-duplicate` — `params` is `{"canonical": "<repo#N>" | null, "cross_repo_option": <close-and-link|transfer|keep-both-link> | null}`; two evidence URLs: the proposal's own issue first, the duplicate sibling second.

**In scope**

- `close` and `close-duplicate` proposals appear in the same confidence-sorted queue as label/priority proposals.
- **Evidence-before-approval guardrail:** a close-action row has no Approve/Reject buttons. It shows a **Review evidence** button (opens the drawer) and a **Skip** button. Approve/Reject for the open proposal live in the drawer's Proposal section — the reviewer cannot approve a close without the full item on screen. (The drawer decide buttons render for every action type; low-stakes rows simply also keep their row-level buttons.)
- Close-action rows are visually flagged (a red action badge in the card header) so they stand out while scanning.
- **Bulk approve stays low-stakes:** "Approve visible rows" approves only the visible `add-label` / `set-priority` proposals, never close actions, and its label says so ("Approve visible label/priority rows").
- **Duplicate sibling in the drawer** (deferred from Plan 3b): when the open proposal is a `close-duplicate`, the drawer's Proposal section renders a summary of the sibling issue from the mirror — title, state, labels, a body snippet, and its GitHub link — plus the `canonical` and `cross_repo_option` params stated in words ("keep repo#N", "cross-repo: transfer").
- A pure helper `review_queue.duplicate_sibling(proposal) -> tuple[str, int] | None` that identifies the sibling from the proposal's evidence URLs (the entry that is not the proposal's own issue), unit-tested offline.

**Out of scope (later specs)**

- The executor that applies approved decisions to GitHub (Plan 4). Approving a close here only writes a decision record; nothing closes on GitHub.
- Keyboard-driven review and editing a proposal's params before approving (Plan 3e, #35). Editing matters most for close-duplicate (wrong canonical), but it needs the 3e input model.
- Side-by-side dual-drawer rendering of both duplicates. The sibling summary plus its GitHub link covers judgment; a second full drawer is layout work with no new information.
- Changing the Plan 2 proposal schema (e.g., embedding the sibling's repo/number in `params`). Existing JSONL records are already on disk; the review layer derives the sibling instead.

**Definition of done**

1. Undecided `close` and `close-duplicate` proposals appear in the queue, sorted by confidence with everything else, and are excluded once their issue closes (existing `_is_closed` filter) or a decision is recorded.
2. A close-action row offers only Review evidence / Skip; Approve/Reject work from the drawer and write the same decision records as row-level buttons.
3. "Approve visible rows" leaves close actions undecided.
4. For a `close-duplicate` proposal, the drawer shows the sibling issue's summary from the mirror, or a "not found in mirror" placeholder, without failing.
5. `review_queue` changes have offline unit coverage; app wiring is verified manually (same stance as Plans 3a–3c).

## 2. What already exists

`review_queue.load_undecided` reads all proposals JSONL records, drops decided IDs, drops unsupported actions, drops issues that are no longer open in the mirror, and sorts by confidence. `SUPPORTED_ACTIONS = {"add-label", "set-priority"}` is the only thing hiding close actions today — `_is_closed` already gives close proposals the right staleness behavior for free (an issue closed by a human after analysis silently leaves the queue).

The app (`review_app/app.py`) renders one Shiny module per row with Approve/Reject/Skip buttons and an `on_decide(proposal, verdict)` callback; an app-level `drawer_state` reactive (`{"repo", "number", "proposal"}` or `None`) drives a `render.ui` drawer that shows the full mirrored item plus a Proposal section (`_drawer_proposal`). `decisions.record` copies `action`/`params` generically, and the dashboard's per-category precision table keys on `action` — both handle the new types with no changes.

## 3. Approaches considered

1. **Widen `SUPPORTED_ACTIONS` and stop.** Minimal diff, but close proposals become one-click approvable from a snippet and get swept up by bulk approve — exactly the risk that got them deferred out of Plan 3a. Rejected.
2. **Widen plus guardrails (chosen).** Same single queue, but close-action rows route judgment through the drawer, and bulk approve is scoped to low-stakes actions. Keeps one review surface, adds the two behaviors that make close review safe.
3. **A separate "Close review" tab.** Cleanest separation of stakes, but splits the reviewer's workflow into two queues and duplicates the row/drawer wiring. The confidence sort already interleaves work sensibly; a badge and a guardrail are enough. Rejected.

## 4. Components

Following the existing 1:1 module ↔ test pattern, UI kept thin:

- **`review_queue.py` (extend)**
  - `SUPPORTED_ACTIONS` becomes `{"add-label", "set-priority", "close", "close-duplicate"}`.
  - `HIGH_STAKES_ACTIONS = frozenset({"close", "close-duplicate"})` — the single definition both the row renderer and bulk approve consult.
  - `duplicate_sibling(proposal) -> tuple[str, int] | None` — parse each evidence URL of the form `https://github.com/{owner}/{repo}/issues/{n}`, return the first parsed `("{owner}/{repo}", number)` (the same `owner/repo` form the mirror and proposals use) that differs from `(proposal["repo"], proposal["issue"])`; `None` when evidence is missing, malformed, or contains only the issue itself.
- **`review_app/app.py` (extend)**
  - `row_ui`: when `proposal["action"]` is high-stakes, the card header gains a red action badge and the button row becomes **Review evidence** (an action button that fires the existing `on_open`) and **Skip**; low-stakes rows are unchanged.
  - `_drawer_proposal` (rendered inside the drawer): gains **Approve** / **Reject** / **Skip** buttons wired to app-level inputs (`drawer_approve` / `drawer_reject` / `drawer_skip` — the drawer is app-level, not a module); server effects call the existing `on_decide(drawer_state proposal, verdict)`, which already closes the drawer for the decided proposal and refreshes the queue.
  - `_drawer_proposal`: for `close-duplicate`, adds a "Duplicate sibling" block — resolves the sibling via `review_queue.duplicate_sibling`, loads it with `drawer.load_item`, and renders title, state line, labels, `review_queue.issue_snippet` of the body, and a GitHub link; "(not found in mirror)" when the mirror lacks it; the block is omitted (with a short note) when no sibling can be derived. Also renders `canonical` / `cross_repo_option` as words. For `close`, `params.reason` already reads clearly from the existing params line.
  - `_approve_visible`: approves `[p for p in queue if p["action"] not in HIGH_STAKES_ACTIONS]`; button relabeled "Approve visible label/priority rows".

Data flow for a close review: queue shows the badged row → **Review evidence** sets `drawer_state` → drawer renders full item + proposal (+ sibling for dupes) → **Approve/Reject/Skip** in the drawer → `on_decide` writes the decision, clears the drawer, refreshes the queue.

## 5. Error handling

- **Issue closed after analysis:** existing `_is_closed` filter drops the proposal from the queue; no decision is recorded (nothing to review).
- **Sibling not derivable** (missing/malformed evidence): drawer shows "(sibling not identified from evidence)" in the Duplicate sibling block; the proposal is still reviewable — the evidence links remain.
- **Sibling not in the mirror** (transferred/deleted): "(not found in mirror)" placeholder plus the GitHub link, mirroring the main drawer's behavior.
- **`canonical` null:** render "canonical: (not stated)" rather than hiding the field — its absence is itself signal about the verdict's quality.

## 6. Testing

- **`review_queue.py`:** `close` / `close-duplicate` records now pass `load_undecided` (and still respect decided-ID and closed-issue filtering); `duplicate_sibling` covering the happy path (second evidence URL), sibling-first ordering, self-only evidence, empty evidence, and malformed URLs; `HIGH_STAKES_ACTIONS` membership stays a subset of `SUPPORTED_ACTIONS` (guards a future action type being added to one set but not the other).
- **`review_app/app.py`:** manual verification (badged close row shows no Approve/Reject; drawer decides write records; bulk approve leaves close rows; sibling block renders for a dupe pair in the mirror) — same stance as Plans 3a–3c: the app is thin wiring over tested modules and the repo has no Shiny UI test harness.

## 7. Open items for review

1. **Drawer decide buttons render for every action type,** not just close actions. This keeps one drawer layout and lets low-stakes reviews finish from the drawer too. If Approve-from-drawer on label proposals feels redundant, hiding the buttons for low-stakes actions is a two-line change.
2. **Skip stays on the close row** so a reviewer can defer without opening the drawer. If even Skip should require looking at the evidence, drop it from the row.
