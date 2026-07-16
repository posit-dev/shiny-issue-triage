# shiny-issue-triage

Automation for triaging issues in Shiny team repositories.

The workflow in `.github/workflows/team-issue-triage.yml` runs weekly and on
manual dispatch. Its allowlist, label taxonomy, safety rubric, and helper
scripts live in `.github/triage/`.

See `.github/triage/README.md` for configuration, required secrets, state
branch behavior, and validation commands.

## Mirror pipeline (P1)

The `triage-verse` CLI (Python, managed with [uv](https://docs.astral.sh/uv/))
mirrors issues, PRs, and comments from every repo in `config/repos.yaml` into
a local SQLite database. GitHub stays the source of truth; the mirror is
derived data and can always be rebuilt.

```bash
uv sync                                      # one-time setup
uv run triage-verse sync --full                # initial backfill (resumable)
uv run triage-verse sync                       # incremental refresh (seconds-minutes)
uv run triage-verse verify-counts              # reconcile against GitHub search
uv run triage-verse analytics export           # burndown series -> .data/analytics.json
uv run triage-verse snapshot publish --dated   # upload to mirror-latest + dated tag
uv run triage-verse snapshot bootstrap         # fresh machine: pull mirror-latest
```

Cursors live in the mirror's `repos` table; `--full` ignores them. The
backfill is resumable: re-running `sync --full` re-upserts idempotently, and
interrupted incremental syncs simply continue from the last cursor.

`config/repos.yaml` ships the pilot trio active (reactlog, shinytest2,
py-shinylive); uncomment the rest of the shinyverse when ready to run the full
fleet.

## Analysis pipeline (P2)

Turns the mirror into triage proposals using local embeddings and a language
model backend. Model and embedder config live in `config/models.yaml`.

```bash
uv run triage-verse embed                         # compute/update embeddings (local, free)
uv run triage-verse analyze --wait                # classify + dedup -> .data/proposals/
uv run triage-verse analyze-status                # in-flight batches + today's spend
```

**Backends:** The default `backend: claude_cli` uses `claude -p` on Claude Code
auth (no API key needed, requires Claude CLI installed and enterprise subscription).
To switch to the Anthropic Batch API, set `backend: anthropic_batch` and provide
`ANTHROPIC_API_KEY` in the environment (see issue #18).

`analyze` is a resumable state machine: re-running it collects in-flight
batches rather than resubmitting, so an interrupted run (or the future
scheduled job) simply continues. Spend is metered to the mirror's `spend`
table and capped by `max_usd_per_day` in `config/models.yaml`. Under
`backend: claude_cli`, each `claude -p` call executes and bills
synchronously; `config/models.yaml`'s `batch.workers` controls how many run
concurrently (default 1, this repo starts at 2). `max_usd_per_day` is
checked before every new dispatch, bounding a tripped budget's overshoot
(and a crash's loss) to at most `workers` items instead of the whole stage;
use `--limit` to additionally bound a single run's spend (each call costs
roughly $0.01-0.02).

## Executor pipeline (P3)

Applies approved review decisions to GitHub and can reverse a batch afterward.

```bash
uv run triage-verse execute                       # dry-run: preview mutations, no changes
uv run triage-verse execute --apply                # apply approved proposals to GitHub
uv run triage-verse undo --batch <id>               # dry-run: preview the reversal
uv run triage-verse undo --batch <id> --apply       # reverse a batch: labels restored, issues reopened, executor comments deleted
```

`execute` is dry-run by default; pass `--apply` to mutate. Each issue is
freshness-checked before mutation, and results append to `.data/results/`.
`undo` is also dry-run by default and reverses a previously executed batch.

## Steady state & automation (P5)

Commands for the continuous triage loop and graduated autonomy system.

```bash
uv run triage-verse state pull                 # merge triage-state branch into local .data/
uv run triage-verse state push                 # push local .data/ back to triage-state branch

uv run triage-verse steady-state               # orchestrate one loop iteration (sync → analyze → execute)
                                               # note: the scheduled workflow ships dormant (cron commented out)

uv run triage-verse tier1                      # run "already fixed?" sessions, capped per day
uv run triage-verse tier2 owner/repo#N         # label an issue for an AI draft-PR attempt

uv run triage-verse autonomy status            # show per-category precision + promotion eligibility
uv run triage-verse autonomy status --write    # write promoted categories → config/autonomy.yaml

uv run triage-verse execute --auto             # auto-apply promoted categories with spot audits
```

`execute --auto` writes synthetic `auto-approved` decisions for categories that
meet the precision threshold, deterministically samples a fraction for human
audit, then executes them. The review app's **Audit** tab lists executed
audit-flagged items for confirm/reject; rejecting records a precision failure
and prints the undo command.

Design: `docs/superpowers/specs/2026-06-12-shinyverse-issue-triage-design.md`.
Open followups: `docs/superpowers/plans/2026-06-12-triage-verse-followups.md`.

## Machine-readable output (`--json`)

Every command accepts a global `--json` flag, in either position
(`triage-verse --json sync` or `triage-verse sync --json`). With it, the command
prints a single JSON envelope on stdout and sends all human/progress logging to
stderr:

    {"command": "sync", "ok": true, "exit_code": 0,
     "data": {"repos": 2, "issues": 4, "prs": 2, "comments": 6}}

- `ok` is `true` when the command ran to completion. A command that ran fine but
  reports a negative result (e.g. `verify-counts` found a mismatch) is still
  `ok: true`, with a non-zero `exit_code` and the details in `data`.
- Bad input or an unexpected error gives `{"ok": false, "error": "..."}` with a
  non-zero `exit_code`.
- `exit_code` mirrors the process exit code, so shell callers can branch on
  either the field or `$?`.
