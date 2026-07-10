---
name: verify
description: Build/launch/drive recipe for verifying triage-verse changes end-to-end (review app, CLI). Use when verifying a code change actually works at its runtime surface.
---

# Verifying triage-verse changes

Everything runs under `uv run` — the system Python's sqlite3 lacks extension
loading, so `db.connect` raises outside the uv-managed interpreter.

## Review app (Shiny)

Seed a throwaway `.data` tree, then launch on a fixed port and drive with a
browser (Playwright MCP works well):

```bash
SEED=$(mktemp -d)
# seed with a small python script: db.connect(f"{SEED}/mirror.sqlite"), then
# db.upsert_issue / upsert_comment / upsert_pr rows, con.commit(), and write
# proposal records ({id, repo, issue, action, params, rationale, confidence,
# evidence}) to $SEED/proposals/2026/W27.jsonl. Only SUPPORTED_ACTIONS on OPEN
# issues appear in the queue; proposal ids must be [A-Za-z0-9_] only (they
# become Shiny module ids — hyphens crash the queue render).
TRIAGE_VERSE_DB=$SEED/mirror.sqlite \
TRIAGE_VERSE_PROPOSALS=$SEED/proposals \
TRIAGE_VERSE_DECISIONS=$SEED/decisions \
  uv run shiny run src/triage_verse/review_app/app.py --port 8321
```

Flows worth driving: queue renders sorted by confidence; row title opens the
slide-over drawer (`#drawer-panel`); Close button and backdrop dismiss it;
Approve/Reject/Skip append to `$SEED/decisions/YYYY/Www.jsonl` and remove the
row; a proposal whose issue is missing from the mirror shows "(not found in
mirror)"; PR items show "PR · MERGED" and deep-link to `/pull/N`.

Gotchas:

- The drawer backdrop intercepts pointer events — Playwright clicks on queue
  elements behind it time out. Dismiss first (`document.getElementById('drawer_close').click()`)
  or dispatch the click via `browser_evaluate`.
- A favicon.ico 404 in the console is pre-existing noise, not a finding.
- Kill the server with `pkill -f "shiny run src/triage_verse/review_app/app.py"`.

## CLI

`uv run triage-verse --help` lists subcommands; they default to `.data/` paths
and take the same env overrides as the app.
