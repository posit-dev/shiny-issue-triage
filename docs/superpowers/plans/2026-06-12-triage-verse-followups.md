# Triage Hub — Followups for Barret

Items surfaced during P1 execution (subagent-driven, 2026-06-12) that are **not blockers** but warrant your attention or a decision. None are severe. Grouped by urgency.

## Known limitation that affects the P1 exit criterion

0. **`verify-counts` drift is one-directional and un-prunable (deleted/transferred/spam-hidden issues).** The incremental sync is upsert-only and re-enters an issue only when its `updatedAt` bumps. An issue that is **deleted, transferred out, or hidden as spam** on GitHub never re-appears in the sync window, so its `state='OPEN'` row lives in the mirror forever. Because `verify-counts` measures `mirror − github`, this drift only ever pushes the mirror *above* GitHub, and it accumulates over the backlog's lifetime. The `--tolerance` default of 2 (now a CLI flag) absorbs small drift, but on a 42-repo multi-year backfill it can eventually exceed any fixed tolerance and make the reconciliation gate report a mismatch for reasons unrelated to sync correctness. **For P1: when running the exit-criterion check, a mirror over-count of a few issues is expected, not a sync bug.** **P2 fix options:** (a) add a periodic "list all open issue numbers per repo, mark mirror rows absent from GitHub as a synthetic closed/`gone` state" reconciliation pass, or (b) make `verify-counts` accept a proportional tolerance. Caught by the final whole-implementation review, not the per-task reviews. Related: `verify-counts` currently reconciles **open issues only** — closed-issue, PR, and comment totals are synced but never reconciled; widen if you want those dimensions gated too.

## Needs a human decision / real-world action

1. **~~Snapshot publish/bootstrap has never run against real GitHub.~~ DONE (2026-06-29).** Validated end-to-end against `posit-dev/shiny-issue-triage` Releases: `snapshot publish --dated` created `mirror-latest` + `mirror-2026-06-29`, and `snapshot bootstrap` downloaded + decompressed an exact-matching copy (619 issue/PR rows, 833 comments, 92 open). The release upload/download path works.

2. **GitHub App installability for the full fleet is unverified.** `config/repos.yaml` ships the pilot trio active (reactlog, shinytest2, py-shinylive) and the rest of the ~40-repo shinyverse commented out. Before uncommenting, confirm the `posit-shiny-automation` GitHub App (or your chosen token) can read every target repo — especially cross-org ones (`r-lib/*`, `ramnathv/htmlwidgets`, `plotly/plotly.R` which the spec already flagged, `schloerke/shinyjster`). Some may need the app installed or may simply be unreachable. This is config-only, no code change.

3. **`sync` is single-threaded across repos.** A full backfill of ~40 repos (rstudio/shiny alone is ~6k issues + ~100k comments) runs sequentially and could take a few hours. That's the documented expectation and it's resumable via cursors, but if you want it faster, parallelizing across repos (each repo is independent) is a natural future enhancement. Deferred intentionally — not built. Flagging so the first full backfill's wall-clock time isn't a surprise.

## Low-priority code cleanups (safe to defer)

4. **~~No linter/type-checker configured.~~ DONE.** `ruff` (lint + format) and `pyright` are now dev deps, wired through the Makefile (`make py-check-format` / `py-check-types`) and CI; both pass clean. The codebase was reformatted to ruff style. The two cosmetic hints noted earlier (`cli.py`'s `"db.sqlite3.Connection"` string return hint; `sync_all`'s bare `-> dict`) are accepted by pyright as-is, so they're optional polish, not required.

5. **`_prune_dated` hardcodes `--limit 100`** for `gh release list`. Fine while `keep=8` (the list stays tiny), but undocumented. A one-line comment or deriving the limit from `keep` would future-proof it if snapshot retention is ever raised substantially.

## Notes (no action needed, just FYI)

6. **Two production bugs were caught by review and fixed during execution**, both the same class — code invoking `gh repos/...` instead of `gh api repos/...`: the comment-sync fix (commit `19d22c5`) and a pre-emptive patch to the plan's Task 11 text (commit `31c2ee4`) so verify-counts wouldn't inherit it. Mentioning because it's a pattern worth watching for in later phases: anything calling `gh.gh_json`/`gh.run_gh` must pass `["api", <path>]`, not `[<path>]`.

7. **The full backfill is one SQLite transaction per repo** (single commit at end of each repo's sync). For rstudio/shiny's ~100k comments that's a large WAL buffer but well within memory. If GitHub Actions memory ever becomes a constraint, periodic intra-repo commits are the lever. Not a P1 concern.
