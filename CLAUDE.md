# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`shiny-issue-triage` automates triaging issues across the Shiny team
repositories. It has two largely independent layers:

- **`.github/triage/`** — the GitHub Actions workflow
  (`.github/workflows/team-issue-triage.yml`, runs weekly + on dispatch) and its
  allowlist, label taxonomy, safety rubric, and helper scripts (a mix of Python
  and Node `.mjs`). See `.github/triage/README.md`.
- **`triage-verse`** — a Python CLI (`src/triage_verse/`, managed with
  [uv](https://docs.astral.sh/uv/)) that mirrors GitHub into a local SQLite
  database, analyzes issues into triage proposals, and applies approved
  decisions back to GitHub.

## Commands

The `Makefile` mirrors CI (`.github/workflows/ci.yml`); run `make` for the full
target list.

- `make py-check` — **primary gate for Python work**: ruff format check + lint,
  pyright types, pytest. Run this before considering Python changes done.
- `make py-format` — auto-fix lint and formatting (ruff).
- `make check` — everything CI runs: `validate-yaml`, `compile-scripts`,
  `py-check`, and `js-check` (Node tooling). Run this if you touched
  `.github/triage/` scripts or config YAML.

Run a single test:

- Python: `uv run pytest tests/triage_verse/test_foo.py::test_bar`
- Node: `node --test tests/test_process_triage_actions.mjs`

Launch the Shiny review app: `shiny run src/triage_verse/review_app/app.py`
(reads the mirror at `$TRIAGE_VERSE_DB`, default `.data/mirror.sqlite`).

The CLI is `uv run triage-verse <subcommand>`; the `README.md` walks the whole
pipeline with copy-pasteable invocations.

## Architecture

The `triage-verse` pipeline is a chain of stages that each read and write a
**local SQLite mirror** (`.data/mirror.sqlite`) plus append-only JSONL
artifacts under `.data/`. GitHub is always the source of truth; everything under
`.data/` is derived data that can be rebuilt.

Flow: **sync → embed → analyze → (human review) → execute → undo?**, with
`steady-state` orchestrating one full loop.

- **sync** (`sync.py`) mirrors issues/PRs/comments into SQLite. Per-repo cursors
  live in the mirror's `repos` table, so incremental syncs resume from the last
  cursor and `--full` re-upserts idempotently.
- **embed** (`embed.py`) computes local, free vector embeddings (fastembed +
  sqlite-vec) used for duplicate-candidate detection.
- **analyze** (`analyze.py`) is the heart: a **resumable multi-stage state
  machine** (candidates → classify → dedup → recheck) that produces proposals in
  `.data/proposals/`. Re-running collects in-flight work rather than
  resubmitting, so an interrupted or scheduled run simply continues. The LLM
  backend is pluggable (`llm.py`, `classify.py`): default `claude_cli` shells out
  to `claude -p`; `anthropic_batch` uses the Batch API. Spend is metered to the
  mirror's `spend` table (`spend.py`) and hard-capped by `max_usd_per_day`,
  checked before every dispatch.
- **review app** (`review_app/`, Shiny for Python) is the human-in-the-loop UI
  over proposals; decisions are written as JSONL to `.data/decisions/`.
- **execute** (`executor.py`) applies approved decisions to GitHub (dry-run by
  default; `--apply` to mutate), freshness-checking each issue and appending
  outcomes to `.data/results/`. **undo** reverses a batch.
- **autonomy** (`autonomy.py`) tracks per-category precision from reviewed
  decisions and graduates categories into `config/autonomy.yaml`;
  `execute --auto` then auto-approves those categories with sampled spot audits.
- **state bus** (`state.py`): `.data/` JSONL + cursors are synced across machines
  and CI via a dedicated orphan git branch (`triage-state`), not committed to
  `main`. `state pull` / `state push` move data to and from that branch.

Config lives in `config/`: `models.yaml` (backend, model/embedder, `workers`,
spend caps), `repos.yaml` (repos to mirror — ships with a pilot subset active,
rest of the shinyverse commented out), `autonomy.yaml`, and `templates/`.

## Logging-verbosity convention

**This codebase prefers lots of logging.** Long-running or multi-stage
operations must make progress unambiguous from the console (or a redirected log
file) alone — a reader should never wonder whether a long run is progressing or
stuck. For any new long-running or multi-stage work:

- **Log the start and end of every stage** with a clear marker (e.g.
  `stage: <name> - starting` / `- done (...)` / `- skipped (...)` /
  `- halted on budget`). `analyze.py` is the reference implementation.
- **Emit a periodic heartbeat inside any wait/poll/retry loop** so output never
  goes silent long enough to look hung.
- **Keep stdout line-buffered** for anything that may run in the background,
  under a scheduled job, or redirected to a file. Python block-buffers stdout
  when it isn't a TTY, so tailable logging won't flush until exit unless you
  reconfigure it (see `cli.main()`).

Introduced in `analyze.py` (commit `a1bf876`); applies codebase-wide.

## Design docs, plans, and decisions

Three folders hold the project's written history. Read them for context before
changing a subsystem; **treat them as historical records — do not rewrite them
after the fact.**

- `docs/superpowers/specs/` — **design specs**: what a feature should do and why.
- `docs/superpowers/plans/` — **implementation plans**: the step-by-step build.
- `decisions/` — **architecture-decision records** (see `decisions/README.md`).

House rules:

- **Each document stands alone.** A reader should understand it without opening
  another file, and without section-number cross-references ("see section 5") —
  restate the context you need in prose.
- **Specs and plans record what was decided at the time.** If the design
  changes, write a new one rather than editing the old to match what shipped.
- **Decisions are immutable; only status changes.** When revisited, add a new
  record and mark the old one `Superseded by <new file>`.
