# Plan 4: Executor — apply approved decisions, with undo

**Date:** 2026-07-13 · **Issue:** posit-dev/shiny-issue-triage#10 · **Status:** approved design

The executor is the single component that mutates GitHub. It reads human-approved
decisions from the decisions log, re-checks freshness per issue, applies allowlisted
mutations via the `gh` CLI, and records enough in a results log to reverse any
executed batch. This is a Python rewrite inside `triage_verse` (decided in
brainstorming); the old Node scripts in `.github/triage/scripts/` are a pattern and
test-case source, not a dependency.

## Decisions locked in brainstorming

- **Python, not Node.** New modules under `src/triage_verse/`; port the allowlist
  validation and its test matrix from `process-triage-actions.mjs` tests.
- **Plain `gh` default auth.** Mutations post as the operator (Barret). The GitHub
  App token router is deferred until execution moves into the scheduled Action
  (Plan 5).
- **Closes always comment.** Every `close` / `close-duplicate` posts a templated
  comment (full text below) before closing. Comment IDs are recorded so undo can
  delete them.

## CLI surface

```
triage-verse execute [--apply] [--repo OWNER/NAME] [--limit N]
triage-verse undo --batch <id> [--issue OWNER/NAME#N] [--apply]
```

- **Dry-run is the default for both.** Without `--apply`, each planned mutation is
  printed and a result record with `status: "dry-run"` is written; no `gh` mutation
  runs. (Read-only `gh` calls — the freshness fetch — do run in dry-run mode, so a
  dry run reports which items would bounce as stale.)
- `--repo` restricts to one repository; `--limit` caps the number of decisions
  processed in the run.
- Mutation pacing: 1 second sleep between mutating `gh` calls (injectable for
  tests).
- Directories follow the existing env-var conventions:
  `TRIAGE_VERSE_DECISIONS` (default `.data/decisions`),
  `TRIAGE_VERSE_RESULTS` (default `.data/results`),
  `TRIAGE_VERSE_DB` (default `.data/mirror.sqlite`).

## Input selection

1. Read all decision records from the decisions log (`decisions/YYYY/Www.jsonl`
   under the decisions dir).
2. Keep verdicts `approved` and `edited` (an `edited` record's `params` already
   holds the reviewer's edited values; `proposed_params` is the original).
3. Take the **latest decision per `proposal_id`** (by `decided_at`) — a proposal
   re-reviewed after a stale bounce may have several decisions.
4. Join each decision back to its proposal record (by `proposal_id`, from the
   proposals log under `TRIAGE_VERSE_PROPOSALS`, default `.data/proposals`) —
   decision records don't carry `issue_updated_at`, the freshness baseline lives
   on the proposal. A decision whose proposal can't be found is an `error` result.
5. Drop any decision whose `id` already appears as `decision_id` in the results
   log with a **final** status (`applied`, `stale-needs-rereview`, or `error`).
   `dry-run` records are not final — a dry run never blocks a later `--apply`.
   This makes `execute` idempotent: re-running skips completed work.

## Freshness contract

Before mutating an issue, re-fetch that one issue
(`gh api repos/{repo}/issues/{number}`) and compare its `updated_at` to the
proposal's recorded `issue_updated_at`.

- **Match** → proceed with the mutation.
- **Moved** → no mutation. Write a result record with
  `status: "stale-needs-rereview"` and stop processing that decision.

Multiple mutations by the same run against the same issue (comment then close) are
one logical action; the freshness check runs once per decision, before its first
mutation. Our own comment moving `updated_at` mid-decision is expected and ignored.

### Stale resurfacing in the review queue

`review_queue.load_undecided()` currently hides any proposal that has a decision.
It gains an optional results-dir parameter: a proposal whose **latest event**
(comparing decision `decided_at` vs. result `executed_at`) is a
`stale-needs-rereview` result is treated as undecided and resurfaces in the queue,
flagged stale (the row shows a "stale" badge with the old vs. new `updated_at`).
The review app passes `TRIAGE_VERSE_RESULTS` through. Approving the resurfaced
proposal writes a fresh decision, whose newer timestamp supersedes the stale
result — but note the *proposal's* `issue_updated_at` is unchanged, so the
executor will bounce it again unless the pipeline re-emitted the proposal.
Resurfaced rows therefore exist mainly to route the item back through
classification (reject/skip) or to confirm the action is still wanted after
re-reading the issue in the drawer; the normal path for a still-valid action is
the next pipeline run re-proposing it with a fresh `issue_updated_at`.

## Mutations (allowlist — nothing else, ever)

| Decision action | `gh` mutation(s) | Params used |
|---|---|---|
| `add-label` | `gh issue edit --add-label <label>` | `params.label` |
| `set-priority` | `gh issue edit --add-label <priority label>`, removing any other `Priority: *` label the issue carries | `params.priority`, mapped to a `priority` entry in `.github/triage/labels.yaml` |
| `close` | post templated comment → `gh issue close --reason {completed\|"not planned"}` | `params.reason` is the classifier enum {`fixed`, `answered`, `stale`, `not-planned`, `duplicate`}: `fixed`/`answered` → GitHub reason `completed` + close-completed template; `stale`/`not-planned` → GitHub reason `not planned` + close-not-planned template; `duplicate` → error (duplicates must arrive as `close-duplicate` proposals with a canonical target) |
| `close-duplicate` | post templated comment linking canonical → close as duplicate via `gh api graphql` `closeIssue(stateReason: DUPLICATE, duplicateIssueId: …)` | `params.canonical` (repo + number); cross-repo pairs fall back to `--reason "not planned"` + the cross-repo template variant, since GitHub's duplicate close is same-repo only |
| `reopen` | `gh issue reopen` | undo-only; never proposed |

Validation before any mutation:

- Labels (`add-label`, `set-priority`) must appear in `.github/triage/labels.yaml`
  (classification names, priority names, or `allowed_safe_output_labels`).
  Unknown label → `status: "error"`, no mutation.
- `close` reasons restricted to the two enum values. `close-duplicate` requires a
  non-null canonical target.
- Comment bodies are rendered only from the templates below with a fixed
  placeholder set. Proposal `rationale` (model output) is never posted.

After each applied mutation, update the mirror row (`issues` table: labels, state)
so the local view stays consistent without waiting for the next sync.

## Comment templates (`config/templates/`)

Placeholders are the complete set: `{canonical_url}`. Everything else is fixed
text. Templates are validated at load: unknown placeholder → error before any
mutation.

**`close-completed.md`**

> This issue appears to have been resolved, so we're closing it as completed as
> part of a maintainer-reviewed triage of the backlog.
>
> If you're still seeing this with the latest release, please leave a comment and
> we'll gladly reopen it.

**`close-not-planned.md`**

> As part of a maintainer-reviewed triage of the backlog, we've decided not to
> move forward with this issue, so we're closing it as not planned.
>
> If you think this deserves another look, please leave a comment and we'll gladly
> reopen it.

**`close-duplicate.md`**

> This looks like a duplicate of {canonical_url}, so we're closing this one to
> consolidate the discussion there. This close was reviewed and approved by a
> maintainer as part of a triage of the backlog.
>
> If your report differs from that issue, please leave a comment and we'll gladly
> reopen it.

**`close-duplicate-cross-repo.md`** — same as above, plus a sentence noting the
canonical issue lives in another repository and inviting the reporter to follow it
there.

## Results log

Appended to `.data/results/YYYY/Www.jsonl` (gitignored, same weekly layout as
proposals/decisions, via `jsonl_log.append_weekly`). One record per decision
processed:

```json
{
  "id": "<uuid>",
  "batch_id": "<uuid, one per execute run>",
  "decision_id": "…", "proposal_id": "…",
  "repo": "rstudio/shinytest2", "issue": 123,
  "action": "close-duplicate", "params": { … },
  "status": "applied | dry-run | stale-needs-rereview | error",
  "error": "<message, when status=error>",
  "prior": { "labels": ["bug"], "state": "open", "state_reason": null },
  "comment_id": 456789,
  "executed_at": "2026-07-13T18:00:00Z"
}
```

`prior` captures the issue's pre-mutation labels and state (from the freshness
fetch — no extra API call); `comment_id` is set when a comment was posted. Together
these are sufficient for undo. Errors on one decision don't abort the batch; the
run continues and exits non-zero if any record ended in `error`.

## Undo

`triage-verse undo --batch <id>` loads that batch's `applied` records and reverses
each, newest first:

| Applied action | Reverse |
|---|---|
| label added (`add-label`, `set-priority`) | remove that label; re-add any `Priority: *` label that `set-priority` removed (recorded in `prior.labels`) |
| issue closed | `gh issue reopen` |
| comment posted | `gh api -X DELETE repos/{repo}/issues/comments/{comment_id}` |

- Dry-run by default, `--apply` to perform — same convention as `execute`.
- `--issue OWNER/NAME#N` limits the undo to one issue within the batch.
- Undo results are appended to the same results log with `action: "undo"` and a
  `undoes_result_id` field, so undos are auditable and themselves idempotent
  (an already-undone record is skipped).
- Undo does **not** freshness-bounce: it only removes artifacts this tool created
  (its labels, its comment, its close). If a reversal fails because the artifact is
  already gone (e.g., someone deleted the comment), record `status: "error"` for
  that item and continue.
- Issue transfers are out of scope entirely (not proposable, not undoable), per the
  master spec.

## Module layout

- `src/triage_verse/executor.py` — selection, freshness check, dispatch, results
  records, undo. Pure logic takes a `run_gh`-shaped callable; no direct
  `subprocess` use.
- `src/triage_verse/templates.py` — load + validate + render `config/templates/`.
- `src/triage_verse/cli.py` — `execute` and `undo` subcommands.
- `src/triage_verse/review_queue.py` — stale-resurfacing extension.

## Testing (pytest, faked `run_gh`, no network)

- **Dry-run snapshot:** fixture decisions + mirror → exact planned-mutation output
  and `dry-run` result records; assert zero mutating `gh` calls.
- **Freshness bounce:** fixture issue's `updated_at` moved between proposal and
  execution → `stale-needs-rereview`, no mutation; queue resurfaces the proposal.
- **Undo round-trip:** execute a fixture batch (label + close + comment) against a
  stateful fake `gh` → undo → original labels/state/comments restored.
- **Allowlist rejection matrix:** unknown label, bad close reason, missing
  canonical, unknown template placeholder → `error` records, no mutations
  (cases ported from `tests/test_process_triage_actions.mjs` /
  `test_dry_run_triage_actions.mjs`).
- **Idempotency:** re-running `execute` after a partial batch skips `applied` /
  `error` / stale decisions, retries nothing silently.
- **Latest-decision-wins** and **edited-params** selection tests.

## Out of scope (deferred)

- GitHub App token router / bot identity → Plan 5, when execution moves to CI.
- Scheduled or automatic execution; graduated-autonomy auto-apply → Plan 5.
- Issue transfers; PR mutations of any kind.
- Retrying `error` records automatically (inspect and re-decide instead).
