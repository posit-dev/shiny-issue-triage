# Plan 2 Analysis Pipeline — Smoke-Run Runbook

**Date:** 2026-06-29  
**Status:** PENDING (manual run required)

## Overview

This runbook documents the smoke test for the analysis pipeline (P2), which turns the mirror into triage proposals using local embeddings and the Anthropic Batch API. The test validates end-to-end functionality on the shinytest2 pilot repo.

**IMPORTANT:** This is a manual, one-time run that requires:
- `ANTHROPIC_API_KEY` set in the environment
- Network access to the Anthropic Batch API
- Expected cost: pennies (< $0.05 per run)

This run has NOT yet been performed. A maintainer with `ANTHROPIC_API_KEY` must execute it.

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

**Expected:** Records parse as valid JSON and contain an `action` field with one of: `close_duplicate`, `close_invalid`, `label_and_wait`, `no_action`.

### 6. Total cost summary
Record the total USD spent (sum of all rows in the `spend` table).

**Expected:** < $0.05 (pennies; if notably higher, investigate batch efficiency).

## Results — PENDING (manual run required)

**Date run:** [Not yet performed]

**Maintainer:** [To be filled by runner]

**Cost:** [To be filled by runner]

### Spend summary
```
[Run the command from Verification Checklist #1 and paste results here]
```

### Cached tokens
```
[Run the command from Verification Checklist #2 and paste results here]
```

### Classification count
```
[Run the command from Verification Checklist #3a and paste results here]
```

### Dedup verdict count
```
[Run the command from Verification Checklist #3b and paste results here]
```

### Proposals files found
```
[Run the command from Verification Checklist #4 and paste results here]
```

### Sample proposal records
```
[Run the command from Verification Checklist #5 and paste a few sample records here]
```

### Breaker status
Was the daily spend limit (`max_usd_per_day` in `config/models.yaml`) reached?

**Result:** [Not yet run — expected: No]

### Overall result
- [ ] Spend > 0 USD
- [ ] Cached tokens > 0
- [ ] Classifications populated
- [ ] Dedup verdicts populated
- [ ] Proposals JSONL valid
- [ ] Breaker did not trip
- [ ] All checks passed

**Status:** [To be filled after run]
