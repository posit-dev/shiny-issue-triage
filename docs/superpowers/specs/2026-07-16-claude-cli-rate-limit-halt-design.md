# Design: rate-limit-aware halt for the `claude_cli` analyze backend

**Issue:** #24 — *claude_cli: back off on rate-limit failures instead of burning
the content-retry budget*

## Problem

The `claude_cli` backend (`ClaudeCliClient` in `src/triage_verse/llm.py`) runs
one `claude -p` subprocess per analyze request. Its `submit_one` method has a
two-attempt loop whose purpose is to recover from *content-quality* failures —
the model returning prose instead of JSON, or JSON that fails schema validation.
On the second failed attempt the item is returned with status `errored`; because
`analyze._apply_result` stores nothing for a non-`succeeded` result and analyze
re-selects any issue lacking a stored classification (keyed by a content hash),
an errored item is simply retried on a future `analyze` run.

Today every failure is funneled into that same two-attempt budget. In
particular, `_default_runner` raises a `RuntimeError` on any non-zero process
exit, which `submit_one` catches as one failed attempt (`cli-call-failed`).

That conflates two very different failure kinds. A rate limit is a
*predictable, load-driven* failure, not a random content glitch. Issue #19 added
a bounded worker pool (`cfg.workers`, currently 2) that runs several `claude -p`
subprocesses concurrently against the same subscription, so a rate limit is
likely to strike several in-flight items at once. Spending both content-retry
attempts (and the real dollars each attempt costs) re-hitting a wall that will
not move for seconds-to-days is pure waste.

## What `claude -p` actually does under a rate limit

Established by research against the current CLI (documented behavior plus
empirical envelope captures; confidence noted where it matters):

- **The CLI already retries transient limits itself.** By default it performs up
  to 10 retries with exponential backoff on HTTP 429 / 529, honoring
  `retry-after`, before surfacing any failure. Env vars `CLAUDE_CODE_MAX_RETRIES`
  (default 10) and `CLAUDE_CODE_RETRY_WATCHDOG=1` (retry 429/529 nearly
  indefinitely, intended for CI) tune this. **Consequence:** a rate-limit failure
  that actually reaches our subprocess is a *sustained* limit, not a momentary
  blip the CLI would have absorbed.

- **On a rate-limit failure with `--output-format json`, the CLI prints its
  result envelope to stdout *and* exits non-zero.** The envelope carries the
  signal:
  - `is_error: true`
  - `api_error_status`: the HTTP status (e.g. `429` or `529`); `null` on success.
  - `result`: a human-readable error string.
  - `subtype`: **varies by CLI version** (observed both `"error"` and
    `"error_during_execution"`), so it is *not* a reliable key.

- **Two flavors of "rate limit" exist**, and both warrant the same response here:
  - *Transient throttle / overload* — `429 · temporary capacity`,
    `529 Overloaded`, `Server is temporarily limiting requests`. Resets in
    seconds; the CLI already backed off before we saw it.
  - *Subscription usage limit* — `You've hit your weekly limit · resets Mon`,
    `session limit · resets 3:45pm`, `Opus limit`. Resets in minutes to days.

- **Anything not a rate limit keeps its current handling.** Genuine crashes,
  timeouts, and content-quality failures still consume the two-attempt budget
  and error out as they do today.

**Current-code gap that blocks detection:** `_default_runner` discards stdout and
raises on non-zero exit, so the error envelope (with `api_error_status`) is
destroyed before `submit_one` can inspect it. Detection requires surfacing that
envelope on a non-zero exit.

## Decision: halt the stage on any detected rate limit

When a rate limit is detected, **halt the current analyze stage**, exactly the
way the daily-budget breaker (`spend.breaker_tripped`) already halts it: stop
dispatching new items, let the run end without finalizing, and leave the
un-dispatched issues to resume on the next `analyze` invocation (they have no
stored classification, so they are re-selected automatically).

This is deliberately simpler than a per-item back-off-and-retry loop. Two
reasons make halting the better fit:

1. The CLI already absorbs transient blips, so a failure that reaches us usually
   means "the account is limited right now — come back later," not "wait two
   seconds and try again."
2. Resumption is already free via the content-hash cache, and once one call is
   rate-limited, every other queued call would hit the same wall. Halting stops
   the waste immediately without any new waiting machinery.

### Rejected alternatives

- **In-run back-off + requeue (the issue's literal suggestion).** A per-item
  sleep-and-retry loop, optionally with a cross-worker backoff signal. Rejected:
  a sustained usage limit resets in minutes-to-days, so sleeping inside the run
  either barely helps (short sleep) or blocks a worker thread for far too long
  (long sleep), and the CLI has already done the short-timescale backoff that
  would actually help. Halting achieves the same end state (the item is retried
  later) with less code and no risk of a stuck run.

- **Setting `CLAUDE_CODE_RETRY_WATCHDOG=1` in the subprocess env.** Would make the
  CLI retry 429/529 nearly indefinitely. Rejected: against a multi-hour usage
  limit this is precisely the hung run we want to avoid, and it would also block
  behind `_CLI_TIMEOUT`. We keep the CLI's default retry count untouched.

- **Keying detection on `subtype`.** Rejected: the value varies across CLI
  versions. We key on `api_error_status` and a substring match of the error
  string instead.

## Detailed design

### 1. Surface the error envelope on non-zero exit (`_default_runner`)

Change `_default_runner` so a non-zero exit **with non-empty stdout** returns
that stdout (the JSON error envelope) for the caller to interpret, and only
raises when a non-zero exit produced **no** stdout (a genuine crash/timeout —
preserving today's diagnostic, including truncated stderr). The injectable-runner
contract used by tests (a runner returning a plain string) is unchanged.

### 2. Rate-limit predicate (`llm.py`)

Add a small, defensive, easily-extended predicate:

```
def _is_rate_limit(envelope: dict) -> bool
```

Returns true when either:
- `envelope.get("api_error_status")` is in `(429, 529)`, or
- `envelope.get("is_error")` is truthy **and** the error string
  (`envelope.get("result")`, coerced to `str`) matches, case-insensitively, any
  of a maintained pattern list: `rate limit`, `429`, `529`, `overloaded`,
  `usage limit`, `temporarily limiting requests`, `hit your … limit`.

The pattern list lives as a module-level constant so it is trivial to extend as
real-world strings are observed.

### 3. New `BatchResult` status `"rate_limited"` (`submit_one`)

Inside `submit_one`'s attempt loop, after parsing the envelope:

- If `_is_rate_limit(envelope)` — **return immediately** with
  `status="rate_limited"`, carrying `cost_usd` and usage (tokens were spent, so
  spend stays accurate) and the error string. Crucially, this does **not** loop,
  so the two-attempt content budget is left untouched.
- Else if `envelope.get("is_error")` is truthy (a non-rate-limit error, e.g. a
  billing or auth failure) — record it as a failed attempt (`cli-error: …`) and
  `continue`, matching today's "burn an attempt then error" behavior. This also
  guards against feeding a `null`/error `result` into the JSON extractor.
- Else — the existing success path: extract JSON, validate against the schema,
  return `succeeded`.

`BatchResult.status`'s comment is updated to list `rate_limited`.
`classify.parse` / `dedup.parse` already return `None` for any non-`succeeded`
status, so no proposal is stored for a rate-limited item and it resumes next run
— no change needed there.

### 4. Halt propagation in `analyze.py`

Add `summary["halted_on_rate_limit"]` alongside the existing
`summary["halted_on_budget"]`, and a helper `_is_halted(summary)` returning
`halted_on_budget or halted_on_rate_limit`. Every place that currently gates on
`summary["halted_on_budget"]` (skipping the dedup and recheck stages, and
skipping proposal-writing / `finish_run`) switches to `_is_halted(summary)`, so
no gate is missed.

Both synchronous submission paths detect the new status:

- **`_submit_stage_parallel`** — when a completed future returns a
  `rate_limited` result: record its spend (so the daily total stays accurate),
  do **not** insert/collect a batch row for it (leave the item unstored so it
  re-queues), set `halted = True` so no new items are dispatched, and let the
  already-in-flight futures drain and record normally (their spend is committed
  and they may still succeed — the same treatment the budget breaker gives
  in-flight items). Return `False`.

- **`_submit_stage` sequential branch** — after collecting a chunk, if the
  collected result was `rate_limited`, return `False`. (`_try_collect_batch`
  already records spend and stores nothing for a non-`succeeded` result, so the
  only new behavior is the halt signal.)

A stage returning `False` sets `summary["halted_on_rate_limit"] = True` at the
call site (parallel to how `halted_on_budget` is set today), which — via
`_is_halted` — skips the remaining stages and finalization and leaves the run
open to resume.

### 5. Logging

Per the codebase logging convention, the rate-limit halt logs a clear,
distinct marker (e.g. `stage: <name> - halted on rate limit (<n>/<total> done)`),
separate from the budget marker, so a tailed log shows *why* a run stopped.
`cli.py`'s analyze summary output (which currently prints `halted_on_budget`) is
extended to report a rate-limit halt distinctly.

## Testing

Unit tests (`test_llm_cli.py`, reusing the existing `_envelope` helper) for
`submit_one` with a fake runner returning:
- a 429 `api_error_status` envelope → `rate_limited`, exactly one runner call
  (budget not burned), cost recorded;
- an `is_error` envelope whose `result` says "hit your weekly limit" → same;
- a plain non-rate-limit `is_error` envelope → still burns both attempts and
  returns `errored`;
- a success envelope → `succeeded` (unchanged).

Plus a `_default_runner`-level test that a non-zero exit **with** stdout returns
the stdout, and a non-zero exit **without** stdout still raises.

Stage-level tests (`test_analyze.py`) with a synchronous fake client scripted to
return `rate_limited`:
- a `rate_limited` result halts dispatch, leaves the affected issue unstored
  (so it re-queues) and sets `summary["halted_on_rate_limit"]`, and skips dedup /
  recheck / proposal-writing;
- in the parallel path (`cfg.workers > 1`), items already in flight when the
  rate limit lands still complete and are recorded.

## Scope

- **In scope:** the `claude_cli` backend only. The `anthropic_batch` backend
  never returns `rate_limited` (its provider handles limits server-side), so
  `_is_halted` and the collection paths simply never see the new status there.
- **Out of scope:** any cross-worker sleep/backoff coordination, tuning the
  CLI's own retry env vars, and changing the content-quality two-attempt budget.
