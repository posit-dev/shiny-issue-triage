# Claude Sonnet 5 over Sonnet 4.6 for recheck/dedup (despite a real, unexplained per-call cost increase)

**Date:** 2026-07-01
**Status:** Decided and implemented (commit `92bd3c9`)
**Related:** GitHub issues #8 (analysis pipeline), #18 (Batch API backend), #21 (breaker granularity); `docs/superpowers/specs/2026-06-29-plan-2-analysis-pipeline-design.md`; `docs/superpowers/specs/2026-06-29-claude-cli-backend-design.md`

## Context

The triage pipeline uses two models: a cheap model (Haiku) that classifies every open issue in bulk, and a stronger model (Sonnet) for two lower-volume, higher-scrutiny jobs — rechecking any classification the cheap model was unsure about or that proposed closing an issue, and adjudicating duplicate-candidate pairs found by embedding similarity. Both Sonnet-tier jobs run per triggering issue, so their per-call cost sets the pipeline's marginal cost for those stages.

Claude Sonnet 5 (`claude-sonnet-5`) was released while this pipeline was mid-implementation, succeeding Claude Sonnet 4.6 (`claude-sonnet-4-6`), which the pipeline had been using. The instruction driving this decision was explicit and unconditional: use the newest model. The pipeline currently reaches Claude through `claude -p` (the `claude_cli` backend — chosen because no Anthropic Console API key is available yet; see the claude-cli-backend design doc), so this decision is about which model string that backend requests, not about the Anthropic API generally.

## What we measured

Two paired, apples-to-apples calls — identical trivial prompt, no custom system prompt, `--output-format json`, differing only in `--model`:

| Model requested | Resolved to (confirmed via `modelUsage` in the response) | Cost |
|---|---|---|
| `--model sonnet` (short alias) | `claude-sonnet-4-6` | $0.114 |
| `--model claude-sonnet-5` (full id) | `claude-sonnet-5` | $0.231 |

Sonnet 5 cost roughly **2x** Sonnet 4.6 for identical input in this test. This also surfaced an unrelated but important fact: on the installed `claude` CLI, the short `sonnet` alias still resolves to the old Sonnet 4.6, not to the newest Sonnet. Requesting the model by alias would have silently kept the pipeline on the previous model.

Separately, we fetched Anthropic's current published pricing (`platform.claude.com/docs/en/about-claude/models/overview.md`, checked 2026-07-01):

| Model | List price (input / output per MTok) |
|---|---|
| Claude Sonnet 4.6 | $3 / $15 |
| Claude Sonnet 5 | $3 / $15, with **introductory pricing of $2 / $10 through August 31, 2026** |

**This is the surprising part: published list pricing says Sonnet 5 is priced the same as, or currently cheaper than, Sonnet 4.6.** The observed 2x cost gap in our probe cannot be explained by the per-token rate — something about how much is sent or generated per call differs between the two models.

## Why the cost differs despite equal-or-lower list pricing (working hypothesis, not confirmed)

The same pricing page states: *"On Claude Sonnet 5, [the `effort` parameter] defaults to `high` on the Claude API and Claude Code."* No equivalent default is documented for Sonnet 4.6 on Claude Code. Our pipeline passes no explicit `--effort` flag, so each model runs at whatever Claude Code defaults it to — and Sonnet 5's documented default is `high`. Higher effort means more internal reasoning before the final answer, which bills as more tokens even for a task as small as "classify this issue as JSON." A newer tokenizer generation could also mean the same text costs more tokens to represent, independent of effort — Sonnet 5 shares its "Jan 2026" knowledge-cutoff generation with Opus 4.8, which uses a tokenizer introduced with Opus 4.7.

We have not isolated which factor is responsible (or ruled out others) — doing so would need another paid probe pair with full token-usage capture, which we judged wasn't worth the extra cost purely for root-causing when the practical, actionable finding — roughly double the per-call cost for this pipeline's typical short structured-output use case — is already clear enough to act on.

One structural point applies to both models equally and is worth understanding regardless of this decision: every `claude -p` call is a fresh, independent process, so the pipeline's system prompt (rubric, label taxonomy, schema instructions) is paid for as a full cache-creation write on **every single call** — there is no cache reuse across calls the way a shared Batch API prefix would give. This inflates the practical cost of both models versus a naive token-count estimate, but does not by itself explain the difference *between* the two models.

## Options considered

### A. Use Claude Sonnet 5 for recheck and dedup — chosen

**Pros**
- Newest, most capable Sonnet-tier model — matches the explicit instruction and the default policy of using the latest model unless there's a reason not to.
- Equal-or-lower *list* price than Sonnet 4.6, with a temporary introductory discount through August 2026 — the sticker price argues for switching, not against it.
- Longer support runway; Sonnet 4.6 is already listed under "Legacy models" in current Anthropic docs.

**Cons**
- In practice, roughly 2x the per-call cost measured for Sonnet 4.6, for reasons not yet isolated (most likely the `effort: high` default, possibly compounded by tokenizer differences) — the opposite of what the list price predicts.
- The gap is measured on two trivial paired calls only; production-content calls (real issue bodies, real comment threads) may behave differently.

### B. Stay on Claude Sonnet 4.6

**Pros**
- Known, previously-validated cost profile — this is what the original smoke test measured: $0.017 for its one production recheck call.
- No unexplained cost regression to carry.

**Cons**
- Contradicts the explicit instruction to use the newest model.
- Sits on a model Anthropic's own docs already flag as legacy, with an unknown remaining support window.
- Forgoes Sonnet 5's capability improvements for no proven quality benefit in this workload.

## Decision

Use **Claude Sonnet 5** (`claude-sonnet-5`) for the recheck and dedup stages, per explicit instruction and the default-to-newest-model policy. The measured cost increase is real, but not disqualifying: recheck and dedup are the two lowest-volume stages in the pipeline (recheck only fires below a confidence floor or on a close-candidate; dedup only fires on retrieved candidate pairs above a similarity threshold), so the multiplier applies to a small fraction of total pipeline volume — bulk classification stays on Haiku, unaffected by this decision.

We also confirmed the `claude -p` CLI exposes an `--effort <level>` flag (and a separate `--max-budget-usd` per-session cap) that the pipeline does not currently use. Explicitly requesting a lower effort level for these structured-JSON-output tasks — which don't need deep reasoning — is a plausible, cheap way to claw back some or all of the observed gap, and is worth trying as a follow-up rather than as a blocking prerequisite to this migration.

## Consequences

- `config/models.yaml` and the `_MODEL_ALIASES` map in `src/triage_verse/llm.py` now request `claude-sonnet-5` for the recheck and dedup stages; classification stays on Haiku, unaffected.
- The `claude_cli` backend must pass the **literal model id**, not the short `sonnet` alias — the installed CLI's `sonnet` alias still resolves to the old Sonnet 4.6, so aliasing would have silently kept the pipeline on the previous model.
- `config/models.yaml`'s `pricing.claude-sonnet-5` entry carries over Sonnet 4.6's rates as an unverified placeholder; it is not load-bearing under the active `claude_cli` backend (which meters from each call's reported cost directly), but must be corrected with real Sonnet 5 batch rates before the `anthropic_batch` backend is ever enabled (issue #18).
- Follow-up worth doing before running this at fleet scale: measure Sonnet 5's cost on real production content (not just trivial probes), and try passing `--effort low` or `--effort medium` to the recheck/dedup calls to see whether it closes the gap without hurting classification quality.
