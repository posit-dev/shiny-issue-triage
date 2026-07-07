# Plan 3a — Review queue design

- **Date:** 2026-07-01
- **Owner:** Barret Schloerke
- **Status:** Design for review, pre-implementation
- **Builds on:** the Plan 1 mirror and Plan 2 proposals pipeline (`src/triage_verse/`); the program design `docs/superpowers/specs/2026-06-12-shinyverse-issue-triage-design.md`
- **Issue:** #9 (Plan 3 — Review app), narrowed scope; see "Relationship to issue #9" below

This document stands alone: it restates the context it needs rather than pointing at section numbers in other files.

## 1. Goal and scope

Plan 2 turns the mirror into a stream of triage proposals (`add-label`, `set-priority`, `close`, `close-duplicate`) appended to `.data/proposals/YYYY/Www.jsonl`. Nothing has reviewed them yet, and nothing writes to GitHub — that gap is what Plan 3 closes. This spec covers the smallest useful slice of that: a **plain Shiny-for-Python app that lets one person (Barret) approve, reject, or skip proposals**, recording each decision to a local append-only log. It reads the mirror and the proposals log; it never mutates GitHub.

**In scope**

- A review queue: all undecided proposals, flat-sorted by confidence (descending — most-confident first).
- Two proposal action types: `add-label` and `set-priority`.
- Per-row **Approve** / **Reject** / **Skip** buttons, plus an **Approve visible rows** bulk action.
- A decisions log (`.data/decisions/YYYY/Www.jsonl`) that every button press writes to, and that the queue reads back to filter out already-decided proposals.
- Enough per-row context (title/body snippet from the mirror, rationale, confidence, a GitHub link) to judge a label or priority proposal without leaving the app.

**Out of scope (later specs)**

- `close` and `close-duplicate` proposal types — these are higher-stakes and deserve the full-evidence drawer (issue body, comment thread, duplicate's sibling issue) before a human judges them, not a queue-row snippet.
- The issue/PR slide-over drawer.
- The analytics dashboard tab (burndown, close-reason mix, spend, precision) — `analytics.py` already computes the series it needs; wiring it into this app is a separate, additive piece of work.
- `shinyreact` / any React frontend. Plain Shiny for Python, server-rendered.
- Keyboard-driven review. Buttons only for now.
- Edit (changing a proposal's params before approving). Deferred with the close actions — editing only matters once there's enough evidence surface to know what a better value would be.
- The executor that actually applies approved decisions to GitHub (Plan 4, already tracked as its own issue).
- A SQLite index of decided proposal ids. At the current and near-term scale (~10k total proposal+decision records) scanning the JSONL trees on every load is fast enough; see "Open items" for the threshold that would change this.

**Relationship to issue #9.** Issue #9 ("Plan 3: Review app") bundles the queue, drawer, dashboard, and decisions log into one item. This spec covers only the queue + decisions log, as agreed in brainstorming: it's the minimal end-to-end vertical (proposal → human decision → logged record) that later specs (drawer, dashboard) build on top of rather than depend on. Issue #9 stays open until all of its bundled scope lands; this is the first of what will likely be two or three specs against it.

**Definition of done**

1. Running `shiny run src/triage_verse/review_app/app.py` against a `.data/` directory containing real proposals and a real mirror shows an ordered, filtered queue and lets every row be approved, rejected, or skipped.
2. Every decision is durably recorded in `.data/decisions/YYYY/Www.jsonl` and never shown again in the queue after a restart.
3. `review_queue.py` and `decisions.py` have full offline unit coverage; no network, no model calls, no Shiny test harness required.

## 2. What already exists

Plan 2 provides `proposals.py`, whose `write()` appends records shaped `{id, repo, issue, action, params, rationale, confidence, evidence, issue_updated_at, run_id, model}` to `.data/proposals/YYYY/Www.jsonl` (atomic write: temp file + replace). The mirror (`db.py`) has `issues(repo, number, title, body, state, ...)` and related tables. The CLI (`cli.py`) establishes the `.data/` layout convention: `DEFAULT_DB = ".data/mirror.sqlite"`, `DEFAULT_PROPOSALS = ".data/proposals"`. This spec adds `DEFAULT_DECISIONS = ".data/decisions"` to that convention and a new `review_app/` package; it does not modify the CLI or any Plan 1/2 module.

## 3. Components

Following the existing 1:1 `src/triage_verse/<name>.py` ↔ `tests/triage_verse/test_<name>.py` pattern, with the UI kept as thin as possible so the decision logic is unit-testable without a browser:

- **`review_queue.py`** (pure, no Shiny import) — `load_undecided(proposals_dir, decisions_dir, con) -> list[dict]`: reads every `.jsonl` file under both trees, builds the set of already-decided `proposal_id`s, filters them out of the proposals list along with any proposal whose issue is no longer open in the mirror (`con`, a `sqlite3.Connection`), and returns the remainder sorted by `confidence` descending. Malformed lines are skipped with a logged warning, not a crash.
- **`decisions.py`** (pure, no Shiny import) — mirrors `proposals.py`'s `write()`: `record(proposal, verdict) -> dict` builds a decision record; `write(records, decisions_dir, today=None) -> Path` appends them to `.data/decisions/YYYY/Www.jsonl` with the same atomic temp-file-then-replace pattern Plan 1/2 already use.
- **`review_app/app.py`** (thin) — a standalone Shiny-for-Python app (`app_ui`, `server`) that imports the two modules above, renders the queue, and wires button clicks to `decisions.write()` followed by a re-render of `review_queue.load_undecided()`. Run directly: `shiny run src/triage_verse/review_app/app.py`. No new CLI subcommand — this keeps the app a standard Shiny entry point that can be pointed at by `shiny run`, Connect, or any other host without special-casing a custom launcher.

## 4. Decision record schema

```
{ id,             # new uuid, this decision's own id
  proposal_id,    # the id field from the proposals.py record being decided
  repo, issue,
  action, params, # copied from the proposal, so the log is self-contained
  verdict,        # approved | rejected | skipped
  confidence,     # copied from the proposal, for later precision analysis
  decided_at }    # UTC ISO timestamp
```

All three verdicts are written through the same `decisions.write()` path and are treated identically by `review_queue.load_undecided()`'s filter — once a proposal has *any* decision record, it never reappears in the queue. `skipped` is not a snooze; reconsidering a skipped proposal means editing the decisions log by hand. This keeps the filtering logic to one rule instead of three, at the cost of skip being closer in spirit to "no opinion, hide it" than "revisit me later." Flagged in "Open items" below in case that trade surprises you in practice.

There is no `reviewer` field. The program design specifies a single human gate (Barret) for this phase; adding a reviewer identity now would be speculative until multi-reviewer support is real.

## 5. Queue behavior

- **Ordering:** flat list (no grouping by action type), sorted by `confidence` descending — proposals the model was most sure about surface first.
- **Staleness filter:** `load_undecided` also drops any proposal whose issue is no longer `OPEN` in the mirror at load time (checked via `db.get_issue`). A proposal is a static snapshot from whenever the classification pipeline ran (Plan 2 only ever classifies open issues), but the issue may since have been closed on GitHub and re-synced into the mirror; such proposals are stale and are excluded from view rather than shown for review. A proposal whose issue isn't in the mirror at all (a separate, pre-existing case — e.g. deleted/transferred) is still shown with the "not found in mirror" placeholder below, since absence isn't evidence the issue is closed.
- **Row content:** repo + issue number (as a link to the GitHub issue), action + params rendered as text (e.g. `add-label: bug`), confidence, rationale, a truncated title/body snippet looked up from `mirror.sqlite` by `(repo, issue)`. If the issue is missing from the mirror (deleted/transferred — a known, documented edge case from Plan 1), the row shows a "not found in mirror" placeholder instead of failing.
- **Per-row actions:** **Approve**, **Reject**, **Skip** buttons. Each writes one decision record and removes that row from the visible queue.
- **Bulk action:** **Approve visible rows** — approves every row currently rendered in the queue view (i.e., everything not yet filtered out by an existing decision), in one action. There is no confidence-threshold filter control in this first pass; "visible" means "everything the queue is currently showing."

## 6. Configuration

No new config file. `review_app/app.py` reads three paths, each with an environment-variable override so the app can be pointed elsewhere without code changes (relevant once/if it's hosted rather than run locally):

```
TRIAGE_VERSE_DB        default: .data/mirror.sqlite
TRIAGE_VERSE_PROPOSALS default: .data/proposals
TRIAGE_VERSE_DECISIONS default: .data/decisions
```

These defaults match the CLI's existing `DEFAULT_DB` / `DEFAULT_PROPOSALS` constants.

## 7. Testing

- **`review_queue.py`:** fixture JSONL trees (proposals + decisions) in `tests/triage_verse/test_review_queue.py`, covering: undecided proposals returned sorted by confidence; a proposal with any decision record (any verdict) excluded; malformed lines skipped with the rest of the file still parsed; empty proposals/decisions trees handled without error.
- **`decisions.py`:** `tests/triage_verse/test_decisions.py`, covering: `record()` shape and field copy-through from a proposal; `write()` atomicity (temp file replaced, not partially written) and weekly-partition file naming, mirroring the existing `test_proposals.py` coverage for the same pattern.
- **`review_app/app.py`:** left to manual verification for this pass. It has no business logic of its own — it's a thin wire-up over the two tested modules — consistent with there being no existing Shiny UI test harness in this repo yet.

## 8. Open items for your review

1. **Skip is permanent (a decision, not a snooze).** Once skipped, a proposal only reappears if you hand-edit the decisions log. If you'd rather skip be a session-only hide (reappears on app restart, nothing written to the log), that's a different mechanism — say so and I'll adjust before the plan.
2. **JSONL re-scan on every load, no index.** At today's scale (pilot repos, ~10k records) this is a full-parse-every-time approach. If proposal/decision volume grows enough that this becomes slow (rough threshold: file count or total lines growing by an order of magnitude, once the full 42-repo blitz is in scope), the fix is an mtime-aware incremental scan or a small SQLite index — not built now.
3. **No confidence-threshold control on "Approve visible rows."** It approves literally everything on screen. If you want a way to narrow "visible" (e.g., only rows above 0.9 confidence) before bulk-approving, that's a small addition — flag it now or it's a fast-follow.
