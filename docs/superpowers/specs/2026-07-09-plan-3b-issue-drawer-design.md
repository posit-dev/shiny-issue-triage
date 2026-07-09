# Plan 3b — Issue/PR slide-over drawer design

- **Date:** 2026-07-09
- **Owner:** Barret Schloerke
- **Status:** Design for review, pre-implementation
- **Builds on:** the Plan 3a review-queue app (`src/triage_verse/review_app/app.py`) and the Plan 1 mirror (`src/triage_verse/db.py`)
- **Issue:** #32 (Plan 3b — Issue/PR slide-over drawer), child of #9 (Plan 3 — Review app)

This document stands alone: it restates the context it needs rather than pointing at section numbers in other files.

## 1. Goal and scope

The Plan 3a review queue shows each proposal with only a truncated title/body snippet from the mirror. That is enough to judge a label or priority proposal, but judging anything higher-stakes (the `close` and `close-duplicate` proposals coming in Plan 3d) requires the full item: body, comment thread, labels, state. Issue #32 asks for a **GitHub-Projects-style slide-over drawer** that renders the full issue or PR from the local mirror anywhere an item appears in the app, with a deep link out to GitHub. Review should never require leaving the app.

**In scope**

- A right-hand slide-over drawer, opened by clicking an item's title in the review queue, rendering from `mirror.sqlite` (never the network):
  - title, state (with state reason and, for PRs, merged status), author, created/updated/closed timestamps
  - labels, milestone, assignees, reaction count
  - the full body, rendered as Markdown
  - the full comment thread (author, timestamp, Markdown body), oldest first
  - the proposal under review — action, params, confidence, rationale, and its evidence links ("linked evidence")
  - a deep link out to the item on GitHub (`/issues/N` or `/pull/N`)
- A pure data module (`drawer.py`) that assembles all of the above from the mirror, unit-tested offline.
- `db.get_comments(con, repo, issue_number)` and `db.get_pr(con, repo, number)` — the mirror already stores comments and PR metadata; this adds the read paths.
- One drawer instance at app level, reusable by later slices (Plan 3c dashboard, Plan 3d dupe pairs) by setting the same "open item" reactive value.

**Out of scope (later specs)**

- Reviewing `close` / `close-duplicate` proposals (Plan 3d, #34) — the drawer is a prerequisite for them, not the same work.
- The dashboard tab (Plan 3c, #33) and keyboard-driven review + edit (Plan 3e, #35).
- Rendering the duplicate's sibling issue side-by-side (belongs to Plan 3d, where dupe pairs get reviewed).
- Comment pagination or lazy loading. Mirror comment threads for these repos are small; render them all.
- `shinyreact` / any React frontend — same call as Plan 3a: plain Shiny for Python, server-rendered.
- Fetching anything from GitHub at render time. The drawer shows the mirror's snapshot; staleness is bounded by the sync cadence.

**Definition of done**

1. Clicking a queue row's title opens the drawer with the full item from the mirror; the queue stays visible and interactive behind it; a close button (or clicking the backdrop) dismisses it.
2. PRs render with merged/state metadata and deep-link to `/pull/N`; items missing from the mirror show a "not found in mirror" drawer body instead of failing.
3. `drawer.py` and `db.get_comments` have full offline unit coverage; the app wiring stays thin, verified manually (same stance as Plan 3a).

## 2. What already exists

The mirror (`db.py`) has everything the drawer needs: `issues` (title, body, state, state_reason, author, labels_json, assignees_json, milestone, comment_count, reaction_count, is_pr, created/updated/closed_at), `comments` (repo, issue_number, comment_id, author, body, created_at) with an index on `(repo, issue_number)`, and `prs` (merged, merged_at, head_ref, base_ref). There is a `get_issue` read helper but no comments read helper.

The review app (`review_app/app.py`) renders one Shiny module per proposal row; the row header is currently an `<a>` straight to GitHub. Proposal records carry `action`, `params`, `confidence`, `rationale`, and `evidence` (a list of GitHub URLs — one for classification proposals, two for duplicate pairs).

## 3. Approaches considered

1. **Custom slide-over panel in plain Shiny (chosen).** A fixed-position right-hand panel plus backdrop, toggled by a server-side reactive value, content server-rendered via `render.ui`, slide-in via a small CSS keyframe. Matches the Plan 3a app's stack exactly; ~30 lines of CSS, no new dependencies.
2. **Shiny modal (`ui.modal`).** Free behavior (dismiss, backdrop), but a centered modal is not a slide-over: it hides the queue behind it and reads as an interruption rather than a side panel. The issue explicitly asks for the GitHub-Projects pattern.
3. **`shinyreact` client component.** The program design's long-term direction, but Plan 3a deliberately deferred React, and a drawer does not need client-side state. Revisit when something actually requires it.

## 4. Components

Following the existing 1:1 `src/triage_verse/<name>.py` ↔ `tests/triage_verse/test_<name>.py` pattern, UI kept thin:

- **`db.py` (extend)** — `get_comments(con, repo, issue_number) -> list[sqlite3.Row]`, ordered by `created_at` ascending.
- **`drawer.py` (new, pure, no Shiny import)** — `load_item(con, repo, number) -> dict | None`:
  - `None` when the item is missing from the mirror (deleted/transferred — the app shows a placeholder drawer body).
  - Otherwise a dict with the issue columns, plus `labels` and `assignees` parsed from their JSON columns, `comments` as a list of `{author, body, created_at}` dicts, `pr` as `{merged, merged_at, head_ref, base_ref}` when `is_pr` and a `prs` row exists (else `None`), and `github_url` (`https://github.com/{repo}/pull/{n}` when `is_pr`, else `/issues/{n}`).
- **`review_app/app.py` (extend)** —
  - App-level `drawer_state` reactive value: `None` or `{"repo", "number", "proposal"}`.
  - The row module's card header becomes an in-app action link that calls a new `on_open(proposal)` callback (wired the same way as the existing `on_decide`); the GitHub deep link moves into the drawer header.
  - A `render.ui` drawer output: backdrop + fixed right-hand panel (`min(640px, 95vw)` wide, full height, scrollable) rendering the `drawer.load_item` result. Body and comment Markdown render via `ui.markdown` (raw HTML in bodies stays escaped by the CommonMark defaults). A "Proposal" section shows action + params, confidence, rationale, and the evidence URLs as links. A Close button and a backdrop click both clear `drawer_state`.
  - A static `ui.tags.style` block for the panel/backdrop/slide-in CSS.

Data flow: click row title → `on_open` sets `drawer_state` → drawer `render.ui` calls `drawer.load_item` → user reads, optionally follows the GitHub link → Close clears `drawer_state`. Approve/Reject/Skip stay on the row, unchanged; deciding a row while its drawer is open removes the row and closes the drawer (its proposal is no longer under review).

## 5. Error handling

- **Item missing from the mirror:** drawer opens with the header it can build (repo#number, GitHub link) and a "not found in mirror" body — consistent with the queue row's existing placeholder behavior.
- **Empty body / no comments:** render "(no description)" / "(no comments)" placeholders rather than blank sections.
- **`comment_count` disagrees with mirrored comments:** show the mirrored thread and, when `comment_count` exceeds it, a "N comments on GitHub; M mirrored" note — the mirror's comment sync may lag its issue sync.

## 6. Testing

- **`drawer.py`:** in-memory mirror fixtures covering: full issue with labels/assignees/comments (parsed and ordered correctly); missing issue returns `None`; PR row gains `pr` metadata and a `/pull/N` URL; issue without a `prs` row and non-PR issues get `pr=None`; empty/null body and zero comments pass through as empty values.
- **`db.get_comments`:** ordering and repo/issue filtering, in `test_db.py`.
- **`review_app/app.py`:** manual verification (open drawer from a row, scroll a long thread, close via button and backdrop, decide-while-open) — same stance as Plan 3a: the app is a thin wire-up over tested modules and the repo has no Shiny UI test harness yet.

## 7. Open items for review

1. **Deciding a row closes its drawer.** Alternative: keep the drawer open showing the just-decided item for reference. Closing is simpler and matches "the queue is the workspace"; flag if the other behavior feels better in use.
2. **The row header link now opens the drawer, not GitHub.** The GitHub deep link still exists, one click deeper (drawer header). If muscle memory wants a direct external link on the row too, it is a one-line addition.
