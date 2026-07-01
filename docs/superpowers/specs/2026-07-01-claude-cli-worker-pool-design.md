# `claude_cli` worker pool — design

- **Date:** 2026-07-01
- **Owner:** Barret Schloerke
- **Status:** Approved design, pre-implementation
- **Builds on:** the `claude -p` model backend (`docs/superpowers/specs/2026-06-29-claude-cli-backend-design.md`) and the per-item persistence fix for issues #21/#23 (commit `184a486`)
- **Issue:** #19

This document stands alone: it restates the context it needs rather than pointing at section numbers in other files.

## 1. Situation

The `claude_cli` backend runs one `claude -p` subprocess call per issue, fully sequentially — `ClaudeCliClient.submit()` loops over items one at a time, and `analyze._submit_stage` (as of the #21/#23 fix) submits, persists, and collects exactly one item before starting the next. This is correct and durable, but slow: the full `rstudio/shinytest2` smoke run (59 issues) took roughly 20 minutes. At fleet scale (thousands of issues across ~40 repos), sequential execution is the dominant cost in wall-clock time.

This design adds a small, bounded worker pool so multiple `claude -p` calls run concurrently, while preserving the durability and budget-enforcement properties the #21/#23 fix just established.

## 2. Where parallelism lives

A new code path in `analyze.py`, taken only when the batch client identifies itself as `synchronous` (the same marker `ClaudeCliClient.synchronous = True` already carries, checked via `getattr(client, "synchronous", False)`). The non-synchronous (Batch API) path is completely unchanged — it already gets its concurrency for free from the provider's own async processing, so this design touches nothing there.

## 3. Execution model

A bounded `concurrent.futures.ThreadPoolExecutor` with `cfg.workers` worker threads. `subprocess.run()` releases the Python GIL while waiting on the child process, so threads (not `asyncio`, not multiprocessing) are the right tool here — no async rewrite of the call chain is needed.

**Worker unit of work:** `client.submit_one(request) -> BatchResult`. This method already exists on `ClaudeCliClient` and is already called directly by existing unit tests, so this formalizes it as part of the "synchronous client" contract (a client that sets `synchronous = True` is expected to also provide `submit_one`) rather than introducing new surface area.

**Race-freedom:** worker threads only compute — they run a subprocess, parse/validate/retry, and return a `BatchResult`. They never touch the SQLite connection or call `log(...)`. The **main thread** owns dispatch, the budget check, every database write, and all logging — draining completed futures as they resolve and persisting each one before dispatching more work. This avoids SQLite's single-writer constraint entirely, with no locking or connection-per-thread complexity needed.

**Dispatch loop (main thread):**
1. While there is unsubmitted work and fewer than `cfg.workers` futures are in flight: check the budget breaker; if tripped, stop dispatching new work (but do not cancel or abandon already-in-flight futures — they were already started and will already incur cost when they finish). Otherwise, submit the next request to the pool and track its future.
2. Wait for the next future to complete (in *completion* order, not submission order — fine, since classify/dedup/recheck items have no ordering dependency on each other).
3. Persist that one result: insert its `batches`/`batch_items` rows, record spend, apply the result (write to `classifications`/`dedup_verdicts`, increment the run summary), mark the batch `collected`, commit, log progress.
4. Repeat until no work remains and no futures are in flight.

## 4. Budget breaker: an explicit, documented widening

Today (post #21/#23 fix), the breaker is checked before *every single item* dispatches — bounding overshoot to at most one item's cost. With `N` workers, the check has to happen before dispatching a *new* item to a free worker slot, not after each completion, because money already committed to a running subprocess can't be un-spent. This bounds overshoot to **at most `N` items' cost** instead of 1.

This is a real, deliberate trade-off, not a regression to the old "overshoot by up to 500 items" behavior — with the default `workers: 2`, the bound is still small and proportional to a config-controlled worker count. It will be stated plainly in the `ClaudeCliClient` docstring, the README, and the `config/models.yaml` comment (the same three places the #21/#23 fix already documents the per-item guarantee), so nobody reads a stronger promise into it than what's true.

## 5. Durability: same logic, scaled by worker count

A crash can lose at most the `workers` items currently in flight at that moment — never the whole stage — because every *completed* item is persisted before the next one is dispatched. With `workers: 2`, that's "lose at most 2 items' worth of spend" instead of "lose at most 1," which is still a small, bounded, and clearly worse-case-quantifiable exposure.

## 6. Shared persistence logic

The existing sequential path (`_try_collect_batch`, used by the polling `_collect()` calls for both backends) and the new parallel path both need to turn a `BatchResult` into database rows: record spend, apply the result, mark collected. This logic is extracted into one shared helper both paths call, so there is exactly one place that decides how a result becomes durable state, regardless of which submission strategy produced it.

## 7. Configuration

A new `workers` field nested under the existing `batch:` section in `config/models.yaml`, alongside `max_requests_per_batch` and `poll_interval_seconds` (all three are "how a batch stage executes" knobs, and grouping them matches the file's existing thematic sections — `embedding:`, `stages:`, `batch:`, `spend:`):

```yaml
batch:
  max_requests_per_batch: 500
  poll_interval_seconds: 30
  workers: 2
```

`ModelsConfig` gains a matching flat field `workers: int`, defaulting to **1** if the key is absent from a config file (so any config written before this change behaves exactly as it does today — pure sequential execution — with no silent behavior change). The starting value for this repo's own `config/models.yaml` is **2**, per explicit instruction.

`workers` only affects synchronous clients; a non-synchronous client ignores it entirely (the async path's own concurrency comes from the provider, not from this pool).

## 8. What this does not cover (tracked separately)

**Rate-limit backoff (issue #24, deferred on purpose).** Running `claude -p` concurrently increases the chance of hitting Claude Code's own subscription rate limits sooner than sequential calls do. Today, `submit_one` treats every failure identically — a rate-limit response would burn one of its two content-quality retry attempts for no benefit. Distinguishing a rate-limit failure and backing off (rather than immediately retrying or giving up) is real, valuable follow-up work, but is deliberately out of scope here: the pool ships first with a small, conservative `workers: 2`, and the backoff refinement follows once real usage shows how the CLI actually behaves under concurrent load.

## 9. Testing

Network-free, `claude`-free, like the rest of the suite. A fake synchronous client (a small test double whose `submit_one` is controllable per-call, including artificial delays to force real interleaving) exercises:
- Multiple items complete concurrently and are each persisted exactly once, with no duplicate or dropped results.
- The breaker check happens before *dispatching* new work, not after collecting it — proven by seeding spend such that only a known number of items should ever be dispatched, and confirming no more than that number (plus at most `workers - 1` already-in-flight) land in `classifications`.
- `workers: 1` (the default) produces behavior identical to the current sequential path — this is the critical regression check, mirroring the same discipline used for the #21/#23 fix's `test_analyze_resumes_without_resubmitting` check.
- A non-synchronous client's behavior and chunk size are completely unaffected by the `workers` config value.
