# Plan 5: Steady-state automation + escalation tiers

**Date:** 2026-07-13 · **Issue:** posit-dev/shiny-issue-triage#11 · **Status:** approved design

Keeps the backlog drained over time and adds the agentic tiers. Four slices built
serially in one PR: (5a) state bus + steady-state loop, (5b) Tier 1 "already
fixed?" checks, (5c) Tier 2 draft-PR sessions, (5d) graduated autonomy.

## Decisions locked in brainstorming

- **Scheduled loop targets GitHub Actions, but ships dormant.** The workflows have
  `workflow_dispatch` triggers only; the cron block is present but commented out.
  Nothing runs on a schedule until Barret uncomments it.
- **Model auth in CI: `CLAUDE_CODE_OAUTH_TOKEN`** repo secret (subscription token
  for `claude -p`), not the metered API key.
- **`triage-state` branch is the state bus** for the small append-only JSONL state
  (proposals, decisions, results) plus `cursors.json`. Mirror snapshots stay on
  GitHub Releases exactly as today — `mirror-latest` refreshed per run, dated
  restore points unchanged.
- **Tier 2 marker label: `ai-triage:fix-requested`.** The label is the mechanism
  of record ("a maintainer asked for an AI fix attempt"); the CLI and review-app
  button are just two convenient ways to apply it. **Convention: every label this
  program introduces uses the `ai-triage:` prefix** so bot-related labels sort,
  filter, and allowlist together.
- **Nothing in CI mutates GitHub issues in v1.** The executor (and `--auto`) runs
  locally only. CI produces proposals and — for Tier 2 — draft PRs.

## 5a — State bus + steady-state loop

### `triage-verse state push` / `state pull`

Sync `.data/{proposals,decisions,results}/**/*.jsonl` and `cursors.json` with the
`triage-state` orphan branch of this repo.

- **Branch layout** mirrors `.data/`: `proposals/YYYY/Www.jsonl`,
  `decisions/YYYY/Www.jsonl`, `results/YYYY/Www.jsonl`, `cursors.json`.
- **Merge strategy: line-set union per file.** Every JSONL record has a unique
  `id` and is written once, so union-by-exact-line is a valid merge: the merged
  file is (existing lines) + (incoming lines not already present), order
  preserved. Laptop decisions and CI proposals can never conflict.
- **Mechanics:** work in a temporary worktree/clone of the `triage-state` branch
  (created if absent as an orphan branch with an explanatory README). `pull` =
  fetch, union-merge branch → `.data`. `push` = pull first (so we never clobber),
  union-merge `.data` → branch files, commit (`state: <n> new records`), push.
  A push with nothing new is a no-op (no empty commits).
- **`cursors.json`** is exported from the mirror's repo-cursor table on every
  `push` (repo → issues/prs/comments cursors + exported-at timestamp). It is
  informational/auditable; the snapshot remains the authoritative cursor source
  and `pull` does not import it into the mirror.
- Uses `gh`-authenticated `git` via the existing `run_gh`-style subprocess wrapper
  patterns; all git/gh calls injectable for tests.

### `triage-verse steady-state`

One command that runs the whole loop, for CI and for manual local runs:

1. `state pull`
2. `sync` (incremental, all configured repos)
3. `embed` (new/updated only)
4. `analyze` (new/updated only — existing classification cache makes this cheap;
   existing `max_usd_per_day` circuit breaker applies)
5. `tier1 --limit <remaining daily cap>` (5b; skipped with `--no-tier1`)
6. `state push`
7. `snapshot publish`

Prints a one-line summary per stage; exits non-zero if any stage failed, but a
later-stage failure never rolls back earlier completed stages. `--dry-run` prints
the stages without running them.

### Workflow `steady-state.yml` (dormant)

- Triggers: `workflow_dispatch` only; `schedule: cron "0 */12 * * *"` present but
  commented out with a note.
- Steps: checkout → install uv + deps → install `claude` CLI → auth via
  `CLAUDE_CODE_OAUTH_TOKEN` secret → `triage-verse snapshot bootstrap` (from
  `mirror-latest`) → `triage-verse steady-state` → job summary line.
- `GITHUB_TOKEN` permissions: `contents: write` (triage-state branch + releases).
  No issue/PR write permissions — this workflow cannot mutate issues.

## 5b — Tier 1 "already fixed?" checks

### Candidate selection (from the mirror)

Open issues in configured repos where either:

- the latest classification has `close_candidate.reason == "fixed"`, or
- a **merged** PR lists the issue in `closingIssuesReferences` yet the issue is
  still open,

minus issues that already have a Tier 1 proposal (any verdict) or an approved
close decision. Ordered oldest-first. Capped by `models.yaml`
`tiers.tier1_max_per_day` (start 25), counted against Tier 1 proposals already
emitted today.

### Session

`triage-verse tier1 [--limit N] [--repo OWNER/NAME]`, one issue per session:

- Shallow-clone the target repo (default branch) into a cache dir
  (`.data/checkouts/<owner>__<name>`, reused across runs with `git fetch`).
- Run non-interactive `claude -p` **read-only** (allowed tools: file read, grep,
  glob, `git log`/`git show` via a restricted Bash allowlist; no write, no
  network). Prompt: the issue title/body/comments from the mirror + instructions
  to find whether the report maps to a fixed change (NEWS/changelog entries,
  commits, merged PRs) and respond with schema-constrained JSON:
  `{"verdict": "fixed|not-fixed|unclear", "fixed_in": str|null,
  "evidence": [urls/commit shas], "summary": str, "confidence": number}`.
- `verdict == "fixed"` → emit a normal `close` proposal
  (`params: {"reason": "fixed"}`, `origin: "tier1"`, evidence links included,
  confidence from the session) into the proposals log. It flows through the
  standard review queue and Plan 4 executor — Tier 1 never mutates anything.
- `not-fixed` / `unclear` → recorded as a Tier 1 proposal-log entry with
  `action: "no-op"` and the verdict, so the issue isn't re-checked daily and the
  outcome is auditable. (`no-op` is not a reviewable action; the queue ignores it.)
- Cost of each session is recorded to the `spend` table (stage `tier1`) from the
  `claude -p` usage output; the `max_usd_per_day` circuit breaker halts further
  sessions when crossed.

## 5c — Tier 2 draft-PR sessions

### Marking work: `ai-triage:fix-requested`

- Added to `.github/triage/labels.yaml` (workflow section + label spec with
  color/description "A maintainer asked an AI agent to attempt a draft-PR fix").
  Not in `allowed_safe_output_labels` — the pipeline must never propose it;
  only humans apply it.
- Three equivalent entry points, all of which simply add the label:
  1. GitHub UI, manually.
  2. `triage-verse tier2 OWNER/NAME#N` — adds the label via `gh`, prints the
     `gh workflow run` command to kick off the fix.
  3. Review-app drawer button **"Request AI fix"** — same label add via `gh`
     (local app, operator's auth), with a confirmation dialog.

### Workflow `tier2-fix.yml` (manual kick)

`workflow_dispatch` with inputs `issue` (`owner/name#N`, required) and `model`
(choice: sonnet default, opus). Steps:

1. **Guard: label present.** Fetch the issue; abort unless it carries
   `ai-triage:fix-requested` and is open.
2. **Guard: weekly cap.** Count this workflow's successful runs in the trailing
   7 days via `gh run list`; abort if ≥ `tiers.tier2_max_per_week` (start 10).
3. **Token:** mint an installation token for the target repo using the existing,
   tested App-token scripts (`.github/triage/scripts/create-github-app-token-map.mjs`
   / `gh-token-router.mjs`) with the existing App secrets. (Design change from
   the earlier sketch: the `.mjs` scripts are reused as-is in CI rather than
   ported to Python — they are already tested and nothing local needs App
   tokens.)
4. **Fix session:** checkout the target repo, run a Claude Code session
   (auth: `CLAUDE_CODE_OAUTH_TOKEN`) prompted with the issue thread (fetched via
   `gh`) and repo conventions: implement a fix, run the repo's tests if
   discoverable, commit to branch `ai-triage/fix-issue-<N>`.
5. **Draft PR** on the target repo via the installation token: title references
   the issue, body states it was AI-generated at a maintainer's request (the
   label), links the issue, and asks for review. **Never auto-merge; always
   draft.** If the session concludes it cannot produce a credible fix, it posts
   no PR and the job summary says why.
6. Job summary: issue, outcome, PR URL or failure reason.

Tier 2 writes nothing to the proposals/decisions logs — its output is a draft PR
reviewed through normal code review, and the workflow run history is its audit
log.

## 5d — Graduated autonomy

### Eligibility and promotion

- **Only `add-label` and `set-priority` are ever eligible in v1.** `close` and
  `close-duplicate` stay human-gated regardless of precision.
- `triage-verse autonomy status`: per category, compute trailing precision over
  the most recent consecutive human-reviewed decisions
  (approved+edited = success, rejected = failure; skips excluded). Promotion
  requires ≥ `autonomy.min_decisions` (200) reviewed decisions with precision
  ≥ `autonomy.min_precision` (0.98). Thresholds in `models.yaml`.
- Output: prints the table; with `--write`, updates `config/autonomy.yaml`:

  ```yaml
  promoted:
    add-label: { promoted_at: "2026-08-01", confidence_floor: 0.9 }
  ```

  Promotion is therefore an explicit, reviewable commit — nothing auto-applies
  until this file says so.
- **Demotion:** `autonomy status` also counts, per promoted category, (a) audit
  rejections (below) and (b) reopens of issues the executor auto-modified
  (results ledger closes vs. current mirror state — v1 scope: label actions
  don't close issues, so (b) mainly future-proofs the mechanism). If trailing
  precision including these failures drops below the bar, `--write` removes the
  category from `autonomy.yaml` and prints a demotion warning.

### `triage-verse execute --auto`

- In addition to normal approved decisions, selects **undecided** proposals whose
  category is promoted in `config/autonomy.yaml` and whose confidence ≥ the
  category's `confidence_floor`.
- For each, first appends a synthetic decision record
  (`verdict: "auto-approved"`, `decided_by: "autonomy"`), then executes through
  the identical Plan 4 path — freshness check, allowlist, results ledger, undo
  all apply unchanged.
- **Spot audit:** a deterministic 10% sample (`autonomy.audit_rate`) of
  auto-approved decisions is flagged `audit: true`. The review app gains an
  **Audit** section listing executed audit-flagged items with Confirm / Reject
  buttons; Reject records a `rejected` decision (a precision failure for the
  category) and prints the `triage-verse undo --batch <id> --issue …` command to
  reverse it.
- `--auto` without `--apply` dry-runs like everything else (synthetic decisions
  are only written when `--apply` executes them).

## Config additions (`config/models.yaml`)

```yaml
tiers:
  tier1_max_per_day: 25
  tier2_max_per_week: 10
autonomy:
  min_decisions: 200
  min_precision: 0.98
  confidence_floor: 0.9
  audit_rate: 0.10
```

## Testing (pytest, fake `gh`/`git`/`claude`, no network)

- **State bus:** union-merge property tests (idempotent, commutative, no line
  loss); push-pull round trip via a local bare repo fixture; no-op push produces
  no commit.
- **Steady-state:** stage orchestration with injected fakes; a mid-loop failure
  reports non-zero but completed stages persist.
- **Tier 1:** candidate query against a seeded mirror (both selection criteria,
  exclusions); session output parsing (fixed / not-fixed / malformed JSON);
  daily cap and circuit-breaker halts; emitted proposal shape.
- **Tier 2 CLI:** label add call shape; label is not in
  `allowed_safe_output_labels` (regression test).
- **Autonomy:** promotion at exactly 200/0.98 boundaries; demotion on audit
  rejection; `--auto` writes synthetic decisions then executes (round-trip with
  Plan 4 fake gh); audit sampling determinism; `close` categories never eligible.
- **Workflows:** `make validate-yaml` covers both new workflow files; a test
  asserts the cron trigger is absent/commented (dormancy regression test).

## Out of scope (deferred)

- Activating any cron schedule (one-line uncomment when ready).
- CI-side execution of issue mutations (executor stays local; App-token use in
  CI is limited to Tier 2 draft PRs).
- Tier 2 self-selection of issues, auto-merge, or non-draft PRs.
- Multi-tenancy packaging (Phase 6 of the master spec).
