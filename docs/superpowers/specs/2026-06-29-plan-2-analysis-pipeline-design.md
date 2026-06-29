# Plan 2 — Analysis pipeline design

- **Date:** 2026-06-29
- **Owner:** Barret Schloerke
- **Status:** Design for review, pre-implementation
- **Builds on:** the Plan 1 mirror (`src/triage_verse/`); the program design `docs/superpowers/specs/2026-06-12-shinyverse-issue-triage-design.md`; the embedding-runtime record `decisions/2026-06-29-embedding-runtime-fastembed.md`
- **Issue:** #8

This document stands alone: it restates the context it needs rather than pointing at section numbers in other files.

## 1. Goal and scope

Turn the mirror into triage **proposals** — cheaply and in bulk. This is the first phase that spends model tokens. The pipeline reads only the mirror, computes local embeddings to find duplicate candidates, runs batched model calls to classify issues and adjudicate duplicate pairs, meters every token of spend, and writes proposals to a local append-only log. It never mutates GitHub.

**In scope**

- Local embedding index over each issue's title and body (open and closed, all mirrored repos), stored with `sqlite-vec`, using `fastembed`.
- Duplicate-candidate retrieval (nearest neighbors) feeding a Sonnet adjudication batch.
- Classification with a Haiku batch, plus a Sonnet recheck for low-confidence or close-candidate issues.
- Spend metering to the existing `spend` table, with a `max_usd_per_day` circuit breaker.
- Proposals emitted as local weekly-partitioned JSONL.
- Schema-constrained model outputs as the prompt-injection guardrail.
- Offline, network-free tests (fake embedder, fake batch client) plus one small real smoke run on `rstudio/shinytest2`.

**Out of scope (later plans)**

- The review app (Shiny for Python + shinyreact) and the executor that mutates GitHub.
- Publishing logs to a `triage-state` git branch — proposals stay local for now.
- Tier 1 / Tier 2 agent sessions.
- The scheduled steady-state Action. We build the single-pass entry point it will call; wiring the schedule is a later plan.

**Definition of done**

1. Every stage implemented behind a testable interface; the offline suite passes with no network and no model download.
2. One real smoke run on a small `shinytest2` slice proves the whole chain end to end: real `fastembed`, real Batch API, structured output parsed and validated, `spend` rows written with non-zero USD, the circuit breaker respected, and a valid proposals JSONL file produced. The measured cost (expected: pennies) is recorded.

## 2. What already exists

The Plan 1 mirror provides: `issues`, `prs`, `comments` tables; `repos` cursors; `runs` and `spend` tables (defined, currently unused); the `gh` CLI wrapper; the `config.load_repos` loader; and an argparse CLI (`triage-verse sync | snapshot | analytics | verify-counts`). The `spend` table is `spend(run_id, stage, model, input_tokens, cached_tokens, output_tokens, usd, at)`. Plan 2 adds tables, a config file, a model layer, and `embed` / `analyze` CLI commands.

## 3. Data model additions

All new tables are created idempotently alongside the existing schema in `db.py`.

**Embeddings and change detection**

- `issue_vectors(id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, number INTEGER, embed_hash TEXT, updated_at TEXT, UNIQUE(repo, number))` — one row per embedded issue, with the content hash that produced the vector.
- `vec_issues` — a `sqlite-vec` virtual table `USING vec0(embedding float[384])`, keyed by the same integer `id` as `issue_vectors`. Nearest-neighbor search runs here; results join back through `id`.

`embed_hash = sha256(title + "\n" + body)`. An issue is re-embedded only when its `embed_hash` differs from the stored value.

**Batch state machine**

- `batches(batch_id TEXT PRIMARY KEY, run_id TEXT, stage TEXT, provider_batch_id TEXT, status TEXT, request_count INTEGER, submitted_at TEXT, ended_at TEXT, error TEXT)`. `stage` ∈ {`classify`, `recheck`, `dedup`}; `status` ∈ {`submitted`, `ended`, `collected`, `failed`}.
- `batch_items(batch_id TEXT, custom_id TEXT, target_json TEXT, PRIMARY KEY(batch_id, custom_id))` — maps each request's short `custom_id` back to its target (an issue ref, or a pair of refs). Avoids encoding long `owner/name#number` strings in `custom_id`.

**Result caches** (so we never re-pay for unchanged content)

- `classifications(repo, number, clf_hash, type, priority, assessment, labels_json, close_candidate_json, confidence, model, run_id, at, PRIMARY KEY(repo, number))`. Holds the *final* classification (post-recheck when a recheck happened). `clf_hash = sha256(title + body + concatenated comment bodies)`; reclassify only when it changes.
- `dedup_verdicts(repo_a, number_a, repo_b, number_b, hash_a, hash_b, verdict, canonical_json, cross_repo_option, confidence, rationale, model, run_id, at, PRIMARY KEY(repo_a, number_a, repo_b, number_b))`. Pairs are stored in a canonical order (sort by `(repo, number)`) so each appears once; `hash_a`/`hash_b` are the two sides' `embed_hash` at adjudication time. Re-adjudicate only when either side's hash changes.

Proposals are **not** a table — they are emitted to JSONL (see section 8).

## 4. Pipeline stages

A full run is six stages; the three model stages run as two waves because the recheck depends on classification output.

0. **Embed (local).** For every issue whose `embed_hash` is missing or changed, compute a 384-dim vector via the embedder and upsert into `issue_vectors` + `vec_issues`.
1. **Candidate retrieval (local).** For each *open* issue, take the top `candidate_top_k` neighbors from `vec_issues`, keep those with cosine similarity ≥ `cosine_threshold`, drop self-matches, canonicalize each pair's ordering, de-duplicate, and skip pairs already in `dedup_verdicts` with unchanged hashes. The survivors are the adjudication candidates.
2. **Classify — Haiku batch (Wave 1).** Over open issues lacking a fresh classification, submit a Haiku batch with a schema-constrained output.
3. **Dedup — Sonnet batch (Wave 1, parallel).** Over fresh candidate pairs, submit a Sonnet batch with a schema-constrained output. Independent of stage 2, so it goes out in the same wave.
4. **Recheck — Sonnet batch (Wave 2).** After the Haiku results land, submit a Sonnet batch for every issue whose Haiku result has `confidence < confidence_floor` **or** carries any `close_candidate`, this time with the full comment thread. The Sonnet verdict overwrites the issue's classification. Closes are therefore always double-checked by the stronger model.
5. **Emit proposals (local).** Project `classifications` + `dedup_verdicts` into proposal records and append them to the JSONL log.

## 5. Batch driver (resumable state machine)

The Anthropic Message Batches API is asynchronous: submit a set of requests, poll until the batch ends, then stream results (results arrive unordered — they are keyed by `custom_id`, never by position). Plan 2 drives this as a state machine persisted in the `batches` table so it survives crashes and fits a scheduled, single-pass invocation.

`triage-verse analyze [--repo R] [--limit N] [--full] [--wait]` does, per invocation:

1. Resume or start the run: if uncollected batches from a prior invocation exist, adopt that run (reuse its `run_id`) so an in-flight pipeline is continued rather than duplicated; otherwise `start_run(kind="analyze")`. (The circuit breaker sums spend by UTC day across all runs, so this reuse does not affect budgeting either way.)
2. Run the local stages (0, 1) — they are cheap and always safe to redo.
3. For each model stage that has work and no open batch: **check the circuit breaker**, build requests, submit a batch, and record `batches` (`status=submitted`) + `batch_items`.
4. For each `submitted` batch: retrieve status; when it has `ended`, stream results, route each by `custom_id`, upsert into `classifications` / `dedup_verdicts`, and **log spend per result**. Mark `collected`. Errored/expired/parse-failed results are recorded to a run error list, not crashed on.
5. The `recheck` stage is only built once `classify` is `collected` (the Wave 1 → Wave 2 dependency).
6. With `--wait`: poll on `poll_interval_seconds` until all stages are `collected`, then emit proposals and `finish_run`. Without `--wait`: do one submit/collect pass; if anything is still in flight, persist and exit (a later run resumes — in-flight batches are reloaded from `batches`, never resubmitted). Proposals and `finish_run` happen only when all stages are `collected`.

`custom_id` scheme: short ids (`c{n}`, `r{n}`, `d{n}`) with the real target stored in `batch_items.target_json`.

**Spend metering.** Each result's `usage` becomes one `spend` row: `input_tokens`, `cached_tokens` (from `cache_read_input_tokens`), `output_tokens`, and a computed `usd` from the config pricing (batch rates). `stage` and `model` are recorded too.

**Circuit breaker.** Before submitting *any* batch, sum `spend.usd` for the current UTC day; if it is ≥ `max_usd_per_day`, abort the submit with a clear message, leaving already-collected work intact. Because batch cost is only known *after* results return, the breaker is a between-batch gate, not a mid-batch kill switch. To bound overshoot, each model stage is **chunked** into batches of at most `max_requests_per_batch` requests, and the breaker is checked between chunks — so a tripped budget can be exceeded by at most one chunk's worth of spend.

## 6. Model layer, structured outputs, prompts

A thin `BatchClient` interface wraps the Anthropic SDK: `submit(requests) -> provider_batch_id`, `status(id)`, `results(id)`. Production uses `anthropic`'s `messages.batches`; tests inject a fake. The SDK reads `ANTHROPIC_API_KEY` from the environment automatically, so there is no manual key-handling step when a key is already present.

We deliberately do **not** route this through the `claude` CLI. That CLI is an interactive coding agent, not a Batch API client — it cannot submit the asynchronous, 50%-off, per-result-metered batches the cost model is built on, and the program design mandates a metered API key for every token. So the SDK + Batch API is required here; the CLI's session auth would not give us the batch path or the spend accounting. The one thing worth borrowing from the `gh`-CLI pattern is the ergonomics: like `gh`, we lean on ambient environment auth (the `ANTHROPIC_API_KEY` the SDK already picks up) rather than asking the operator to pass a key in by hand.

**Structured output.** Each request sets `output_config={"format": {"type": "json_schema", "schema": {...}}}` so the model returns JSON matching the stage schema. Each result is parsed and validated against that schema; a validation failure is recorded as a stage error (and, for labels, any value outside the allowlist is dropped and flagged) rather than aborting the batch.

**Prompt caching.** The `system` prompt is a stable prefix — the triage rubric (`.github/triage/issue-triage-rubric.md`) + the resolved label taxonomy (`.github/triage/labels.yaml`) + a short per-repo blurb — with a single `cache_control: {"type": "ephemeral"}` breakpoint at its end. The volatile per-issue content goes in the `messages` after it. All requests in a stage share the identical prefix so the batch reuses the cache; we verify `cache_read_input_tokens > 0` on the smoke run.

**Model IDs:** `claude-haiku-4-5` (classify), `claude-sonnet-4-6` (recheck, dedup).

**Classification schema** (enums aligned to the *real* taxonomy in `.github/triage/labels.yaml`):

```
{ type:        build | chore | ci | docs | feat | fix | perf | refactor | release | style | test | question,
  priority:    Critical | High | Medium | Low,
  assessment:  actionable | needs-info | stale | likely-fixed | out-of-scope,
  labels:      [ subset of the classification allowlist:
                 regression, duplicate, wrong location, needs reprex, needs clarification ],
  close_candidate: null | { reason: duplicate|stale|not-planned|fixed|answered,
                            rationale, confidence },
  confidence:  number 0..1 }
```

`type` mirrors the conventional-commit types this repo already enforces (`.github/workflows/verify-conventional-commits.yaml`: build, chore, ci, docs, feat, fix, perf, refactor, release, style, test), plus `question` for support issues. It is an internal triage dimension, not a GitHub label, so it carries no allowlist constraint.

**Dedup schema:**

```
{ verdict:           duplicate | related | distinct,
  canonical:         issue_ref | null,            # which side is canonical, when duplicate
  cross_repo_option: null | close-and-link | transfer | keep-both-link,  # only for cross-repo pairs
  confidence:        number 0..1,
  rationale:         string }
```

## 7. Configuration

A new `config/models.yaml`, loaded by an extension to `config.py`:

```yaml
embedding:
  model: sentence-transformers/all-MiniLM-L6-v2
  dim: 384
  candidate_top_k: 10
  cosine_threshold: 0.80
stages:
  classify: { model: claude-haiku-4-5,  max_tokens: 512 }
  recheck:  { model: claude-sonnet-4-6, max_tokens: 1024, confidence_floor: 0.70 }
  dedup:    { model: claude-sonnet-4-6, max_tokens: 1024 }
batch:
  max_requests_per_batch: 500     # chunk size → circuit-breaker granularity
  poll_interval_seconds: 30
spend:
  batch_only: true                # bulk stages must use the Batch API
  max_usd_per_day: 50
  pricing:                        # USD per 1M tokens, Batch API (50%-off) rates
    claude-haiku-4-5:  { input: 0.50, cached: 0.05, output: 2.50 }
    claude-sonnet-4-6: { input: 1.50, cached: 0.15, output: 7.50 }
```

Pricing is fully config-driven so it is trivial to correct; the starting numbers come from the 2026-06 batch figures in the program design (`cached` is the 0.1× cache-read rate).

## 8. Proposals output

Proposals append to `.data/proposals/YYYY/Www.jsonl` (ISO year + week), one JSON object per line:

```
{ id, repo, issue, action, params, rationale, confidence,
  evidence: [urls], issue_updated_at, run_id, model }
```

`issue_updated_at` is the freshness token a later executor will re-check before mutating. Action types Plan 2 produces: label additions, priority assignment, and close-candidate (duplicate / stale / not-planned / fixed / answered, with the duplicate's canonical link). Plan 2 does not propose label removals — it has no basis to. Writes are atomic (write to a temp file, then replace), matching the analytics export pattern from Plan 1.

## 9. Prompt-injection guardrail

Issue bodies and comments are untrusted. Three layers contain them:

1. Untrusted text is wrapped in explicit delimiters in the user message, and the system prompt states that issue content is data to analyze, never instructions to follow.
2. Outputs are schema-constrained enums plus free-text rationale; the rationale is stored and (later) displayed, but never executed.
3. Plan 2 emits proposals only — there are no GitHub mutations here — so even a fully subverted output can at most produce a proposal a human reviews later. Labels are additionally validated against `allowed_safe_output_labels`.

## 10. CLI surface

- `triage-verse embed [--repo R] [--full]` — stage 0 only; useful standalone and as a fast feedback loop.
- `triage-verse analyze [--repo R] [--limit N] [--full] [--wait]` — the full pipeline (embeds as needed, retrieves candidates, drives batches, emits proposals). `--limit` caps the number of open issues processed (the smoke run uses it); `--repo` scopes to one repo.
- `triage-verse analyze-status` — prints in-flight batches and today's accumulated spend.

## 11. Testing

Network-free, model-free, deterministic:

- **Fake `Embedder`** — derives a stable vector from the content hash; no model download.
- **Fake `BatchClient`** — returns canned results keyed by `custom_id`, and can simulate `ended` status and errored/expired/parse-failed results.
- **Real `sqlite-vec`** — the extension is a tiny dependency and the vector math should be exercised for real; a startup check asserts the loaded `sqlite3` supports extensions and fails clearly otherwise.

Unit coverage: embed upsert + hash-change recompute; candidate retrieval (top-k, threshold, pair canonicalization, cache skip); classification parse + schema validation + label-allowlist enforcement; recheck-trigger logic (confidence floor / any close_candidate); dedup verdict upsert + cache skip; spend USD math; circuit-breaker gate (accumulated daily spend ≥ cap aborts the next submit); state-machine resume (a persisted `submitted` batch is collected on re-run, never resubmitted); Wave 1 → Wave 2 ordering; proposals JSONL shape; injection delimiting. The existing pytest setup and CI (`make py-check`) extend to cover these.

## 12. Smoke run (one-time, real API)

After the offline suite is green:

1. Ensure `rstudio/shinytest2` is synced into the mirror (Plan 1 `sync`).
2. Confirm `ANTHROPIC_API_KEY` is available — the SDK reads it from the environment automatically, so when it is already set (as it typically is here) there is no manual export step.
3. `triage-verse analyze --repo rstudio/shinytest2 --wait` — the whole repo. shinytest2's open backlog is small (~59 issues), so there is no need to cap it; running it all gives a more representative smoke test of every stage.
4. Verify: `spend` rows with non-zero `usd`; `cache_read_input_tokens > 0` on later requests; populated `classifications` and `dedup_verdicts`; a valid `.data/proposals/...jsonl`; the breaker untripped (set `max_usd_per_day` generously). Record the actual cost in a short notes file.

## 13. Dependencies

Add `anthropic`, `fastembed`, and `sqlite-vec`. `fastembed` brings ONNX Runtime (tens of MB, no PyTorch — per the embedding-runtime decision record); `sqlite-vec` and `anthropic` are small.

## 14. Open items for your review

These are defaults I chose; flag any you want changed before the implementation plan:

1. **Classification enums follow the real `.github/triage/labels.yaml`** (priority `Critical|High|Medium|Low`; classification labels `regression | duplicate | wrong location | needs reprex | needs clarification`) rather than the illustrative schema in the program design. Resolved in review: `type` now mirrors the repo's conventional-commit types (build, chore, ci, docs, feat, fix, perf, refactor, release, style, test) plus `question`. Still my proposal, open to adjustment: the `assessment` set `actionable | needs-info | stale | likely-fixed | out-of-scope`.
2. **Structured output via `output_config.format` (json_schema)** rather than forced tool-use. Confirm.
3. **Pricing numbers** in `config/models.yaml` are the design's 2026-06 batch figures — please sanity-check them before the smoke run, since they drive both the USD log and the circuit breaker.
4. **Circuit breaker is a between-chunk gate** (`max_requests_per_batch: 500`), so a tripped budget can overshoot by at most one chunk. Confirm the chunk size.
5. **Smoke run** = all open issues in `rstudio/shinytest2` (small backlog — decided in review), not a capped slice. The `--limit` flag stays available for other uses.
