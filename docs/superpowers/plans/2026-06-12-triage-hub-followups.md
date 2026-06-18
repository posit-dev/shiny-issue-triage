# Triage Hub — Followups for Barret

Items surfaced during P1 execution (subagent-driven, 2026-06-12) that are **not blockers** but warrant your attention or a decision. None are severe. Grouped by urgency.

## Needs a human decision / real-world action

1. **Snapshot publish/bootstrap has never run against real GitHub.** `src/triage_hub/snapshot.py` is fully unit-tested with faked `gh`, and every `gh release` flag was verified against `gh --help`, but unlike `sync` (which had a live smoke test against `rstudio/reactlog`), the release upload/download path has not touched real GitHub. **Action:** before the first full-fleet blitz, run `uv run triage-hub snapshot publish --dated` then `triage-hub snapshot bootstrap --force` on a scratch checkout and confirm the round-trip. The Task 12 runbook notes this too. Risk if skipped: a flag mismatch surfaces during the first real publish rather than now. Low effort, ~5 min.

2. **GitHub App installability for the full fleet is unverified.** `config/repos.yaml` ships the pilot trio active (reactlog, shinytest2, py-shinylive) and the rest of the ~40-repo shinyverse commented out. Before uncommenting, confirm the `posit-shiny-automation` GitHub App (or your chosen token) can read every target repo — especially cross-org ones (`r-lib/*`, `ramnathv/htmlwidgets`, `plotly/plotly.R` which the spec already flagged, `schloerke/shinyjster`). Some may need the app installed or may simply be unreachable. This is config-only, no code change.

3. **`sync` is single-threaded across repos.** A full backfill of ~40 repos (rstudio/shiny alone is ~6k issues + ~100k comments) runs sequentially and could take a few hours. That's the documented expectation and it's resumable via cursors, but if you want it faster, parallelizing across repos (each repo is independent) is a natural future enhancement. Deferred intentionally — not built. Flagging so the first full backfill's wall-clock time isn't a surprise.

## Low-priority code cleanups (safe to defer)

4. **No linter/type-checker configured.** A couple of spots would benefit once `ruff` + a type checker are added: `cli.py`'s `_open_db` return hint is the string `"db.sqlite3.Connection"` (resolves correctly but is unusual — should become `sqlite3.Connection` with a direct import); `sync_all`'s `-> dict` could be `-> dict[str, int]`. Adding `ruff` to the dev group and a CI step is a reasonable small follow-up task on its own.

5. **`_prune_dated` hardcodes `--limit 100`** for `gh release list`. Fine while `keep=8` (the list stays tiny), but undocumented. A one-line comment or deriving the limit from `keep` would future-proof it if snapshot retention is ever raised substantially.

## Notes (no action needed, just FYI)

6. **Two production bugs were caught by review and fixed during execution**, both the same class — code invoking `gh repos/...` instead of `gh api repos/...`: the comment-sync fix (commit `19d22c5`) and a pre-emptive patch to the plan's Task 11 text (commit `31c2ee4`) so verify-counts wouldn't inherit it. Mentioning because it's a pattern worth watching for in later phases: anything calling `gh.gh_json`/`gh.run_gh` must pass `["api", <path>]`, not `[<path>]`.

7. **The full backfill is one SQLite transaction per repo** (single commit at end of each repo's sync). For rstudio/shiny's ~100k comments that's a large WAL buffer but well within memory. If GitHub Actions memory ever becomes a constraint, periodic intra-repo commits are the lever. Not a P1 concern.
