# `claude -p` model backend — design

- **Date:** 2026-06-29
- **Owner:** Barret Schloerke
- **Status:** Design for review, pre-implementation
- **Builds on:** the Plan 2 analysis pipeline (`src/triage_verse/`, spec `docs/superpowers/specs/2026-06-29-plan-2-analysis-pipeline-design.md`)
- **Related issues:** #8 (Plan 2), #18 (adopt Batch API when a Console key lands), #19 (parallelize `claude -p`)

This document stands alone: it restates the context it needs rather than pointing at section numbers in other files.

## 1. Situation

The Plan 2 analysis pipeline reaches Claude through a `BatchClient` interface (`submit`/`status`/`results`), with two implementations today: a deterministic `FakeBatchClient` (tests) and `AnthropicBatchClient` (the Message Batches API via the `anthropic` SDK). The Batch API path needs an `ANTHROPIC_API_KEY` from the Anthropic Console / Developer Platform.

We do not have a Console API key — only Claude Enterprise and Claude Desktop, which are end-user products and do not issue API keys, and provisioning one is not self-serve. So the Batch path is unusable today, and the pilot (and its required smoke run) is blocked.

The `claude` CLI (Claude Code) *is* installed and authenticated on the operator's machine via the enterprise subscription. It can run one-shot, non-interactive queries with `claude -p ... --output-format json`, which returns the model's answer plus a usage/cost envelope — on credentials we already have. This design adds a third `BatchClient` implementation, `ClaudeCliClient`, that drives `claude -p`, so the pipeline runs now.

**Decision (scope):** additional backend, config-selected — not a replacement. `AnthropicBatchClient` stays; a `backend` config field chooses which client the CLI constructs. The CLI backend is the default now; when a Console key is provisioned (#18) we flip `backend` to `anthropic_batch` for the cheaper, faster, bulk path. The per-call overhead and rate limits of `claude -p` make it a pilot/steady-state tool, not the 4,200-issue blitz engine.

## 2. What we measured

A single trivial Haiku classification through `claude -p --output-format json` returned:
- `result` text wrapped in a Markdown ```` ```json ```` fence (despite instructions to return only JSON),
- `total_cost_usd` = 0.0188, and full `usage` (input/output/cache tokens),

with most of that cost coming from ~19k tokens of Claude Code's own system prompt carried per invocation. Two consequences shape the design: **output is not schema-guaranteed** (we must parse + validate + retry), and **cost is reported per call** (we meter the reported figure). Replacing Claude Code's default system prompt with our own (via `--system-prompt`) is expected to cut most of that overhead; we confirm the saving during the smoke run.

## 3. `ClaudeCliClient`

A new `ClaudeCliClient` in `llm.py`, a peer of `AnthropicBatchClient`, implementing the same interface:

- `submit(requests)` — runs `claude -p` once per request, **sequentially** (parallelism is deferred to #19), and caches the resulting `BatchResult`s under a synthetic batch id it returns.
- `status(provider_id)` — returns `"ended"` (the work already ran synchronously in `submit`).
- `results(provider_id)` — returns the cached `BatchResult`s.

Because it satisfies the interface, the `analyze` state machine, spend metering, proposals, embedding, and CLI are all **unchanged** — they only ever see `BatchClient`.

The subprocess is injected for testability: `ClaudeCliClient(runner=<callable>)`, where `runner(args, input) -> str` defaults to a real subprocess invocation and is replaced by a fake in tests, so CI never calls the real `claude`.

## 4. The `claude -p` invocation

Per request, build the command from the request's existing content:

```
claude -p <user content>
  --model <haiku|sonnet>          # mapped from the stage's config model id
  --output-format json
  --system-prompt <rubric + schema instructions>   # REPLACES Claude Code's default prompt
  --tools ""                      # disable all tools (see injection safety)
  --permission-mode <non-interactive mode>          # never block on a permission prompt
```

- **Model mapping:** the config model ids (`claude-haiku-4-5`, `claude-sonnet-4-6`) map to `claude -p --model` aliases (`haiku`, `sonnet`), which resolve to the same models.
- **`--system-prompt` (replace, not append):** carries our rubric + the JSON-schema instructions, and drops Claude Code's default agent system prompt — cutting the per-call overhead and keeping the model from behaving like a coding agent.
- **Injection safety (critical):** issue and comment text are untrusted. All tools are disabled so injected instructions cannot trigger tool use, file access, or network calls; the model can only emit text. Combined with schema validation of the output (below) and the fact that this pipeline performs no GitHub mutation, an injected input can at most produce a proposal a human later reviews. This is the same containment posture the batch backend has, achieved by disabling tools instead of the API's allowlist. Because getting this right is security-critical, the exact flags that (a) fully disable tools and (b) run non-interactively without a permission prompt are confirmed against `claude --help` and a probe during implementation — `--tools ""` / `--disallowed-tools` for tool disabling, and a non-interactive `--permission-mode` (moot once tools are disabled, set defensively) — and a test asserts the built command actually disables tools.

## 5. Structured output: parse, validate, retry, skip

`claude -p` gives no output-schema guarantee, so `ClaudeCliClient` reconstructs one per call:

1. Read the CLI JSON envelope, take the `result` text, strip Markdown fences, `json.loads`.
2. Validate the parsed object against the stage's JSON schema. That schema already rides in the request at `params["output_config"]["format"]["schema"]` (the batch backend uses it to guarantee output; the CLI backend reuses it to check output), so validation is stage-agnostic. Validation covers required fields and enum membership — without it, `claude -p` could return well-formed JSON with an out-of-vocabulary value.
3. On parse-or-validation failure, **retry once** with a firmer nudge ("return ONLY the JSON object matching this schema, no prose, no fences"); the schema is also embedded in the prompt so the model sees exactly what to produce.
4. If the retry also fails, return a `BatchResult` with `status="errored"`. This flows into the pipeline's existing error path (`classify.parse`/`dedup.parse` return `None`, `_apply_result` skips it, the run records the error). Skipping is non-destructive: results are cached by content hash, so a skipped issue has no stored result and is picked up automatically on the next `analyze` run.

Validation uses the `jsonschema` library (added as a dependency) so the nullable/enum shapes in the existing schemas validate correctly.

## 6. Spend metering

`BatchResult` gains an optional `cost_usd` field. `ClaudeCliClient` fills it from the CLI's reported `total_cost_usd` (authoritative for this backend — it includes the agent overhead a token×price computation would miss) and also carries the reported token counts. `spend.record_spend` uses `cost_usd` when present, and otherwise computes from config pricing (the batch path, unchanged). Both attempts of a retried call are metered — we paid for them. The `max_usd_per_day` circuit breaker reads the summed `spend.usd` exactly as before.

## 7. Configuration and wiring

`config/models.yaml` gains a top-level `backend: claude_cli | anthropic_batch`, default `claude_cli`. `ModelsConfig` gains the corresponding field. A factory `llm.make_batch_client(cfg)` returns the selected implementation, and `_cmd_analyze` uses it instead of hardcoding `AnthropicBatchClient`. Everything else in the CLI is unchanged.

## 8. Testing

Network-free and `claude`-free, like the rest of the suite:

- Inject a fake `runner` into `ClaudeCliClient` that returns canned CLI JSON envelopes (including fenced output, valid output, malformed output, and an enum violation).
- Cover: fence stripping; JSON parse; schema validation catching a bad enum; the one-retry path (fail-then-succeed and fail-then-fail→errored); `cost_usd` captured from `total_cost_usd`; the exact command carries `--tools ""` (tools disabled) and `--output-format json`; model-alias mapping.
- Cover `make_batch_client(cfg)` returning each implementation by config.
- `record_spend` uses `cost_usd` when present and computes from pricing when absent.

The real `claude -p` path is exercised only in the smoke run.

## 9. Smoke run (now unblocked)

With `backend: claude_cli`, the shinytest2 smoke run from the Plan 2 spec runs on the operator's Claude Code auth — no API key:

```
uv run triage-verse sync --repo rstudio/shinytest2
uv run triage-verse analyze --repo rstudio/shinytest2 --wait
```

Verify, and record in the smoke-notes runbook: `spend` rows with non-zero `usd` sourced from `total_cost_usd`; populated `classifications` and `dedup_verdicts`; a valid `.data/proposals/...jsonl`; the breaker untripped. Also record the per-call cost with `--system-prompt` replacement to confirm the overhead reduction.

## 10. Change footprint

- New: `ClaudeCliClient` (in `llm.py`), `llm.make_batch_client`, `jsonschema` dependency.
- Modified: `BatchResult` (+ `cost_usd`), `spend.record_spend` (use `cost_usd` when present), `ModelsConfig` + `config/models.yaml` (+ `backend`), `_cmd_analyze` (use the factory).
- Unchanged: `analyze`, `classify`, `dedup`, `proposals`, `candidates`, `embed`, `db`, and the whole state machine.

## 11. Notes and follow-ups

- **Cost/throughput at scale** is tracked in #18 (adopt Batch API when a key lands) — `claude_cli` is intentionally the interim/pilot backend.
- **Parallelism** is tracked in #19 — `submit` is sequential for now; a bounded concurrent worker pool is the future optimization if `claude_cli` must run at scale.
- **`--system-prompt` savings** are an expectation to confirm empirically in the smoke run, not a guarantee.
