# shiny-issue-triage

Automation for triaging issues in Shiny team repositories.

The workflow in `.github/workflows/team-issue-triage.yml` runs weekly and on
manual dispatch. Its allowlist, label taxonomy, safety rubric, and helper
scripts live in `.github/triage/`.

See `.github/triage/README.md` for configuration, required secrets, state
branch behavior, and validation commands.

## Mirror pipeline (P1)

The `triage-hub` CLI (Python, managed with [uv](https://docs.astral.sh/uv/))
mirrors issues, PRs, and comments from every repo in `config/repos.yaml` into
a local SQLite database. GitHub stays the source of truth; the mirror is
derived data and can always be rebuilt.

```bash
uv sync                                      # one-time setup
uv run triage-hub sync --full                # initial backfill (resumable)
uv run triage-hub sync                       # incremental refresh (seconds-minutes)
uv run triage-hub verify-counts              # reconcile against GitHub search
uv run triage-hub analytics export           # burndown series -> .data/analytics.json
uv run triage-hub snapshot publish --dated   # upload to mirror-latest + dated tag
uv run triage-hub snapshot bootstrap         # fresh machine: pull mirror-latest
```

Cursors live in the mirror's `repos` table; `--full` ignores them. The
backfill is resumable: re-running `sync --full` re-upserts idempotently, and
interrupted incremental syncs simply continue from the last cursor.

`config/repos.yaml` ships the pilot trio active (reactlog, shinytest2,
py-shinylive); uncomment the rest of the shinyverse when ready to run the full
fleet.

> **Before the first full-fleet blitz:** the `snapshot publish`/`snapshot
> bootstrap` round-trip has only been unit-tested with a faked `gh`, not run
> against real GitHub releases. Smoke-test it once on a scratch checkout
> (`snapshot publish --dated` then `snapshot bootstrap --force`) before relying
> on it. See `docs/superpowers/plans/2026-06-12-triage-hub-followups.md`.

Design: `docs/superpowers/specs/2026-06-12-shinyverse-issue-triage-design.md`.
