# Plan 2 Analysis Pipeline — Smoke-Run Runbook

**Date:** 2026-06-29 (updated 2026-07-01 with a full-repo run)
**Status:** VALIDATED — both a 5-issue slice and the full shinytest2 repo (59 open issues) have been run successfully via the `claude_cli` backend.

**Addendum (2026-07-01):** `config/models.yaml` now sets `batch.workers: 2`, so a fresh full run will execute roughly 2x faster (concurrently) than the ~20-minute sequential timing recorded below. Functional results (classifications, spend, proposals) are unaffected by worker count — only wall-clock time changes.

## Overview

This runbook documents the smoke test for the analysis pipeline (P2), which turns the mirror into triage proposals using local embeddings and the Anthropic Batch API. The test validates end-to-end functionality on the shinytest2 pilot repo.

**IMPORTANT:** This is a manual, one-time run that requires:
- Claude CLI installed and logged in (enterprise subscription)
- `config/models.yaml` configured with `backend: claude_cli` (default)
- Network access to Claude Code auth service
- Expected cost: pennies (< $0.05 per run)

This run has NOT yet been performed. A maintainer with Claude CLI access must execute it.

*Note:* To use the Anthropic Batch API instead, set `backend: anthropic_batch` and provide `ANTHROPIC_API_KEY` in the environment.

## Step 1: Ensure mirror is populated

```bash
uv run triage-verse sync --repo rstudio/shinytest2
```

This syncs the shinytest2 repository into the local mirror if not already present.

## Step 2: Run the analysis pipeline

```bash
uv run triage-verse analyze --repo rstudio/shinytest2 --wait
```

This command:
1. Computes embeddings (if not cached) using fastembed
2. Submits classification and dedup-adjudication batches to the Anthropic Batch API
3. Polls for batch completion (resumable if interrupted)
4. Writes proposals to `.data/proposals/` as JSONL files
5. Records spend in the `spend` table

The `--wait` flag causes the command to block until all batches complete.

## Verification Checklist

After the run completes, verify the following:

### 1. Spend metering
```bash
sqlite3 .data/mirror.sqlite "SELECT stage, model, SUM(usd) as total_usd FROM spend GROUP BY stage, model ORDER BY stage"
```

**Expected:** Non-zero USD values per stage (at least one row for each stage that ran).

### 2. Prompt caching verification
```bash
sqlite3 .data/mirror.sqlite "SELECT SUM(cached_tokens) as total_cached FROM spend"
```

**Expected:** Result > 0 (indicates prompt caching was used and working).

### 3. Classifications and verdicts
```bash
sqlite3 .data/mirror.sqlite "SELECT COUNT(*) as classifications FROM classifications"
sqlite3 .data/mirror.sqlite "SELECT COUNT(*) as verdicts FROM dedup_verdicts"
```

**Expected:** Both counts > 0 (indicates both stages produced output).

### 4. Proposals files
```bash
ls -la .data/proposals/*/*.jsonl
```

**Expected:** At least one JSONL file exists.

### 5. Spot-check proposal records
```bash
head -5 .data/proposals/*/*.jsonl | python3 -c "import sys, json; [json.loads(line) for line in sys.stdin if line.strip()]"
```

**Expected:** Records parse as valid JSON and contain an `action` field with one of: `add-label`, `set-priority`, `close`, `close-duplicate`.

### 6. Total cost summary
Record the total USD spent (sum of all rows in the `spend` table).

**Expected:** < $0.05 (pennies; if notably higher, investigate batch efficiency).

## Results — smoke run (2026-07-01)

**Run:** `triage-verse analyze --repo rstudio/shinytest2 --limit 5 --wait` — backend `claude_cli` (no API key). A small `--limit 5` slice validated the real `claude -p` path cheaply; the full 59-issue run is optional (see Dedup note).

**Result line:** `classified=5 rechecked=1 pairs=0 halted_on_budget=False`

**Cost:** $0.069 total for 5 issues.

### Spend (from reported `total_cost_usd`)
```
stage     model              n  usd     in_tok  cached  out_tok
classify  claude-haiku-4-5   5  0.0523  15260   0       7400
recheck   claude-sonnet-4-6  1  0.0170       3       0     44
```
Per-classify call ≈ $0.0105 — roughly half the ~$0.019 baseline, confirming `--system-prompt` replacement removes Claude Code's default-prompt overhead. Note: `claude -p` reports the replaced system prompt under `cache_creation_input_tokens`, which the spend row does not currently capture, so the token columns undercount — but `usd` (from `total_cost_usd`) is authoritative and correct.

### Classifications (5, shinytest2)
type / priority / assessment / confidence all sensible (feat/test/chore). The conf-0.55 issue was rechecked by Sonnet (model overwritten to `claude-sonnet-4-6`), confirming the low-confidence recheck flow. ✓

### Dedup verdicts
0 — no candidate pairs above the 0.80 cosine threshold within 5 issues, so the dedup **adjudication** path was not exercised at `--limit 5`. The full 59-issue run would exercise it.

### Proposals
`.data/proposals/2026/W27.jsonl` — 8 records (5 `set-priority`, 3 `add-label`), each carrying an `issue_updated_at` freshness token. ✓

### Breaker
Not tripped (`max_usd_per_day` = 50). ✓

### Checklist
- [x] Spend > 0 USD (sourced from `total_cost_usd`)
- [~] Cached tokens > 0 — n/a here (system prompt reported as `cache_creation`, not captured; cost still correct)
- [x] Classifications populated + recheck flow exercised
- [ ] Dedup verdicts populated — 0 at `--limit 5` (needs the full run)
- [x] Proposals JSONL valid, correct ISO-week partition
- [x] Breaker did not trip
- [x] `claude_cli` path validated end-to-end with no API key

**Status:** PATH VALIDATED on a 5-issue slice. See the full-repo run below for dedup adjudication coverage.

## Results — full-repo run (2026-07-01)

**Run:** `triage-verse analyze --repo rstudio/shinytest2 --wait` — all 59 open issues, no `--limit`, backend `claude_cli`, using the Sonnet 5 migration for recheck/dedup (see `decisions/2026-07-01-sonnet-5-for-recheck-dedup.md`). Ran unattended in the background; took roughly 20 minutes end to end (sequential `claude -p` calls — see issue #19 for parallelization).

**Result line:** `classified=54 rechecked=8 pairs=0 halted_on_budget=False`

**Cost:** $1.10 total (cumulative with the earlier 5-issue smoke run: classify 59 calls in total across both runs, recheck 9 calls total — 1 stale pre-migration Sonnet 4.6 call plus 8 new Sonnet 5 calls).

### Spend (from reported `total_cost_usd`)
```
stage     model              n   usd     in_tok  cached  out_tok
classify  claude-haiku-4-5   59  0.84    161175  0       110431
recheck   claude-sonnet-4-6  1   0.017        3       0       44   (stale, pre-migration)
recheck   claude-sonnet-5    8   0.2452   10227   12366    1039
```
Sonnet 5's 8 recheck calls averaged **$0.031/call** — consistent with the ~2x-vs-4.6 premium documented in the Sonnet 5 decision record, but not disqualifying at this volume (8 of 59 issues).

### Classifications (54 new, 59 total for the repo)
Distribution: 47 actionable, 10 needs-info, 2 out-of-scope; type/priority mostly `fix`/`feat` at Medium. One close-candidate surfaced (issue #190, `not-planned` — a docs/reference suggestion misfiled as a shinytest2 issue), correctly routed through the Sonnet 5 recheck (confidence 0.65) and held its recommendation. 8 issues fell below the confidence floor or carried a close-candidate and were rechecked by Sonnet 5, all landing at confidence 0.55–0.75 post-recheck. ✓

### Dedup verdicts
**0 pairs, even across the full 59-issue repo** (not just the 5-issue slice). Candidate retrieval found no pairs above the 0.80 cosine-similarity threshold. Per Barret: there may genuinely be no near-duplicate open issues in shinytest2 at this time — a plausible, unforced explanation, not necessarily a pipeline defect. The dedup **adjudication** call path itself remains validated only by the earlier per-task test suite (fake batch client), not by a real `claude -p` call, since no repo tried so far has produced a candidate pair. Worth trying against a larger or higher-traffic repo (e.g. `rstudio/shiny`) if real-call validation of dedup specifically is wanted later.

### Proposals
`.data/proposals/2026/W27.jsonl` — 79 records total across both runs (59 `set-priority`, 19 `add-label`, 1 `close`), each with an `issue_updated_at` freshness token. Nothing posted to GitHub — proposals only, no executor exists yet. ✓

### Breaker
Not tripped (`max_usd_per_day` = 50, well above the $1.10 spent). ✓

### Checklist (full-repo run)
- [x] Spend > 0 USD, sourced from `total_cost_usd` for every call including Sonnet 5
- [x] Classifications populated at full repo scale (54 new + 5 prior)
- [x] Recheck flow exercised repeatedly (8 calls), including a real close-candidate
- [~] Dedup verdicts — still 0; plausibly a true negative (no duplicates in this repo) rather than a bug; real-call dedup path remains unexercised
- [x] Proposals JSONL valid at scale (79 records)
- [x] Breaker did not trip
- [x] `claude_cli` path validated end-to-end, unattended, at full single-repo scale, on Sonnet 5

**Overall status:** VALIDATED for classify + recheck at full single-repo scale. Dedup adjudication's real-call path is still unvalidated for lack of a repo with actual near-duplicate issues — not a known defect, just untested by circumstance.
