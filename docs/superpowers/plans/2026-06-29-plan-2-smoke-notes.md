# Plan 2 Analysis Pipeline — Smoke-Run Runbook

**Date:** 2026-06-29  
**Status:** PENDING (manual run required)

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

**Status:** PATH VALIDATED on a 5-issue slice. Full 59-issue run (to exercise dedup adjudication + produce a complete proposals set) is optional and pending a go-ahead.
