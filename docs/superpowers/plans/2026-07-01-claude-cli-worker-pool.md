# `claude_cli` Worker Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run up to `cfg.workers` `claude -p` calls concurrently for the `claude_cli` backend, cutting wall-clock time for a full analysis run, while preserving the per-item durability and budget-enforcement properties the #21/#23 fix established (just widened from "at most 1 item" to "at most `workers` items").

**Architecture:** A bounded `concurrent.futures.ThreadPoolExecutor` inside a new `analyze._submit_stage_parallel`, taken only for clients with `synchronous = True` and `cfg.workers > 1`. Worker threads only call `client.submit_one(request)` (pure computation, no shared state); the calling thread owns every database write, the budget check (before each new dispatch), and all logging — avoiding SQLite's single-writer constraint by construction rather than working around it.

**Tech Stack:** Python `concurrent.futures` (stdlib, no new dependency) — threads, not `asyncio`, since `subprocess.run()` (what `claude -p` calls go through) releases the GIL while waiting.

## Global Constraints

- Python floor `>=3.11`; all code passes `make py-check` (ruff format+lint, pyright, pytest).
- Tests run with **no network and no real `claude` invocation** — a test-local fake client with a controllable `submit_one`.
- `workers` defaults to **1**, both in `ModelsConfig` and when a config file omits the key — existing configs and the exact current sequential behavior must be unaffected unless someone opts in.
- The non-synchronous (`anthropic_batch`) path and any client without `synchronous = True` must be **provably unaffected** — verified by confirming the entire existing test suite (including `test_analyze_resumes_without_resubmitting`) passes with zero changes to those tests.
- Worker threads touch no shared mutable state and make no `log(...)` calls — only the calling thread writes to `con` or calls `log`.
- The budget breaker is checked before dispatching each *new* item (not after each completion) — this is what makes overshoot bound by `cfg.workers`, not by the whole remaining stage.
- Conventional-commit prefixes: `feat:` / `test:` / `chore:` / `docs:`.

**Design reference:** `docs/superpowers/specs/2026-07-01-claude-cli-worker-pool-design.md`. Related issues: #19 (parallelize — this plan closes it), #24 (rate-limit backoff, deliberately deferred).

## File Structure

- Modify `src/triage_verse/config.py` — add `workers` to `ModelsConfig` + `load_models_config`.
- Modify `config/models.yaml` — add `workers: 2` under `batch:`.
- Modify `src/triage_verse/analyze.py` — extract `_record_and_apply`; add `_submit_stage_parallel`; branch to it from `_submit_stage`.
- Modify `src/triage_verse/llm.py` — update `ClaudeCliClient`'s docstring.
- Modify `README.md` — update the breaker-granularity sentence.
- Tests: `tests/triage_verse/test_models_config.py`, `tests/triage_verse/test_analyze.py`.

---

### Task 1: `workers` config field

**Files:**
- Modify: `src/triage_verse/config.py`
- Modify: `config/models.yaml`
- Test: `tests/triage_verse/test_models_config.py`

**Interfaces:**
- Produces: `ModelsConfig.workers: int` (default `1`, trailing field after `backend`); `load_models_config` reads `batch.workers` with a default of `1` when absent.

- [ ] **Step 1: Write the failing tests**

Add to `tests/triage_verse/test_models_config.py`:

```python
def test_workers_defaults_to_1_when_absent(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "embedding: {model: m, dim: 8, candidate_top_k: 3, cosine_threshold: 0.5}\n"
        "stages:\n"
        "  classify: {model: claude-haiku-4-5, max_tokens: 100}\n"
        "  recheck: {model: claude-sonnet-5, max_tokens: 200, confidence_floor: 0.6}\n"
        "  dedup: {model: claude-sonnet-5, max_tokens: 200}\n"
        "batch: {max_requests_per_batch: 50, poll_interval_seconds: 5}\n"
        "spend: {batch_only: true, max_usd_per_day: 1, pricing: {}}\n"
    )
    assert load_models_config(p).workers == 1


def test_workers_read_from_checked_in_config():
    cfg = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert cfg.workers == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_models_config.py -v`
Expected: FAIL — `test_workers_defaults_to_1_when_absent` with `AttributeError: 'ModelsConfig' object has no attribute 'workers'`; `test_workers_read_from_checked_in_config` with the same error.

- [ ] **Step 3: Implement**

In `src/triage_verse/config.py`, add a trailing field to `ModelsConfig` (after `backend`, matching the existing pattern of adding new optional fields at the end so positional constructions elsewhere keep working):

```python
    backend: str = "claude_cli"
    workers: int = 1
```

In `load_models_config`, read it from the `batch` section with a default:

```python
        backend=data.get("backend", "claude_cli"),
        workers=b.get("workers", 1),
    )
```

In `config/models.yaml`, add the key to the `batch:` section:

```yaml
batch:
  max_requests_per_batch: 500
  poll_interval_seconds: 30
  workers: 2   # concurrent `claude -p` calls under backend: claude_cli; ignored by anthropic_batch
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_models_config.py -v`
Expected: PASS (6 passed — the 2 new plus the 4 already there).

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/config.py config/models.yaml tests/triage_verse/test_models_config.py
git commit -m "feat: add workers config field for the claude_cli backend"
```

---

### Task 2: Extract `_record_and_apply` (pure refactor, no behavior change)

**Files:**
- Modify: `src/triage_verse/analyze.py:254-279` (the current `_try_collect_batch`)

**Interfaces:**
- Produces: `_record_and_apply(con, cfg, run_id, stage, result, target, allowed, summary) -> None` — records spend for `result` (if it has usage) and applies it (writes to `classifications`/`dedup_verdicts`, increments `summary`). Used by both `_try_collect_batch` (unchanged caller) and the new `_submit_stage_parallel` (Task 3).
- Consumes: `spend.record_spend`, `_model`, `_apply_result` (all already exist, unchanged).

This is a pure extraction — no new behavior, no new test needed beyond confirming the existing suite is unaffected (there is no new observable behavior to write a test *for*; the existing suite already exercises `_try_collect_batch`'s behavior end-to-end through `analyze()`, and that is the correct regression check for a behavior-preserving refactor).

- [ ] **Step 1: Run the full suite to record the baseline**

Run: `uv run pytest -q`
Expected: PASS (record the exact count — should be the count from before this plan, e.g. 119 passed).

- [ ] **Step 2: Perform the extraction**

Replace the current `_try_collect_batch` (find it via `grep -n "_try_collect_batch" src/triage_verse/analyze.py`) with:

```python
def _record_and_apply(con, cfg, run_id, stage, result, target, allowed, summary) -> None:
    """Record spend for one result (if it has usage) and apply it."""
    if result.usage is not None:
        spend.record_spend(
            con,
            run_id,
            stage,
            _model(cfg, stage),
            cfg.pricing,
            result.usage,
            cost_usd=result.cost_usd,
        )
    _apply_result(con, cfg, run_id, stage, result, target, allowed, summary)


def _try_collect_batch(con, cfg, run_id, client, allowed, summary, batch, log) -> bool:
    """Collect and apply results for one batch if it has ended. Returns True if collected."""
    if client.status(batch["provider_batch_id"]) != "ended":
        return False
    items = db.get_batch_items(con, batch["batch_id"])
    count = 0
    for result in client.results(batch["provider_batch_id"]):
        target = json.loads(items[result.custom_id])
        _record_and_apply(con, cfg, run_id, batch["stage"], result, target, allowed, summary)
        count += 1
    db.set_batch(con, batch["batch_id"], status="collected", ended_at=db._now())
    con.commit()
    log(f"collected {batch['stage']}: {count} result(s)")
    return True
```

Nothing else in `analyze.py` changes in this task — `_try_collect_batch`'s signature, callers, and observable behavior are identical; only its body's spend-recording lines moved into the new `_record_and_apply` helper.

- [ ] **Step 3: Run the full suite to confirm zero behavior change**

Run: `uv run pytest -q`
Expected: PASS, same count as Step 1, byte-identical pass/fail outcome for every test.

Run: `git diff tests/` (should be empty — this task touches no test files).

- [ ] **Step 4: Run lint/types**

Run: `make py-check`
Expected: ruff + pyright clean.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/analyze.py
git commit -m "chore: extract _record_and_apply from _try_collect_batch"
```

---

### Task 3: Bounded worker pool for synchronous clients

**Files:**
- Modify: `src/triage_verse/analyze.py` (add `import concurrent.futures`; branch in `_submit_stage`; new `_submit_stage_parallel`)
- Test: `tests/triage_verse/test_analyze.py`

**Interfaces:**
- Consumes: `_record_and_apply` (Task 2), `cfg.workers` (Task 1), `client.submit_one(request) -> BatchResult` (already exists on `ClaudeCliClient`; this task formalizes it as part of the "synchronous client" contract).
- Produces: `_submit_stage_parallel(con, cfg, run_id, stage, client, requests, targets, allowed, summary, log) -> bool` (same True/False contract as `_submit_stage`: `True` if the whole stage submitted, `False` if halted on budget).

- [ ] **Step 1: Write the failing tests**

Add to `tests/triage_verse/test_analyze.py`. First, add the two imports the new fixtures need — check the top of the file currently reads `import pathlib` only; change it to:

```python
import json
import pathlib
import threading
import time
```

Then add these fixtures and tests (place them after the existing `_n_issues` helper, before `test_breaker_trips_mid_stage_not_just_between_stages`, or anywhere after `_clf`/`_n_issues` are defined — they depend on both):

```python
class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Usage:
    def __init__(self):
        self.input_tokens = 10
        self.cache_read_input_tokens = 0
        self.output_tokens = 5


class _Msg:
    def __init__(self, payload):
        self.content = [_Block(json.dumps(payload))]
        self.usage = _Usage()


class _ParallelFakeClient:
    """Exposes only submit_one -- the worker-pool primitive. Tracks how many
    calls were simultaneously in flight, to prove real concurrency occurred
    (a non-flaky alternative to asserting on wall-clock timing)."""

    synchronous = True

    def __init__(self, scripted, delay=0.05):
        self.scripted = scripted
        self.delay = delay
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def submit_one(self, request):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        spec = self.scripted[request.custom_id]
        return llm.BatchResult(request.custom_id, "succeeded", message=_Msg(spec))


def test_parallel_dispatch_runs_up_to_workers_items_concurrently(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 4)
    cfg = _cfg(workers=2)
    scripted = {f"c{i}": _clf(0.9) for i in range(4)}
    client = _ParallelFakeClient(scripted, delay=0.05)

    summary = analyze.analyze(
        con,
        cfg,
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )

    assert summary["classified"] == 4
    assert client.max_active == 2  # exactly the worker limit -- proves real overlap
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 4
    rows = con.execute("SELECT status FROM batches WHERE stage='classify'").fetchall()
    assert len(rows) == 4 and all(r["status"] == "collected" for r in rows)


def test_parallel_breaker_blocks_all_dispatch_when_already_over_budget(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 3)
    db.insert_spend(con, "old", "classify", "claude-haiku-4-5", 0, 0, 0, 100.0)
    cfg = _cfg(cap=1.0, workers=2)
    scripted = {f"c{i}": _clf(0.9) for i in range(3)}
    client = _ParallelFakeClient(scripted, delay=0.01)

    summary = analyze.analyze(
        con,
        cfg,
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )

    assert summary["halted_on_budget"] is True
    assert summary["classified"] == 0
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0


def test_parallel_breaker_bounds_overshoot_by_worker_count(tmp_path):
    # Each classify item costs exactly $1.00 (same pricing rig as the
    # sequential breaker test). With a $2.0 cap: the breaker only trips once
    # *already-recorded* spend >= $2.0, which needs at least 2 completed
    # items ($2.00). With workers=2, at most 1 extra item can already be in
    # flight at the moment the 2nd completion crosses the cap (since at most
    # `workers` items are ever in flight at once) -- so completed count is
    # bounded to [2, 2 + (workers - 1)] = [2, 3].
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 5)
    cfg = _cfg(cap=2.0, workers=2)
    cfg.pricing["claude-haiku-4-5"] = {
        "input": 0.0,
        "cached": 0.0,
        "output": 200_000.0,
    }
    scripted = {f"c{i}": _clf(0.9) for i in range(5)}
    client = _ParallelFakeClient(scripted, delay=0.02)

    summary = analyze.analyze(
        con,
        cfg,
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )

    assert summary["halted_on_budget"] is True
    assert 2 <= summary["classified"] <= 3
    assert summary["classified"] < 5  # the breaker had a real effect
```

Note: `_cfg` (already defined earlier in this file) needs a `workers` parameter for these tests to pass it through — that's Step 3 below.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_analyze.py -k parallel -v`
Expected: FAIL — `TypeError: _cfg() got an unexpected keyword argument 'workers'` (since `_cfg` doesn't accept it yet).

- [ ] **Step 3: Update the `_cfg` test helper**

Find `_cfg` near the top of `tests/triage_verse/test_analyze.py`. Change its signature and the `ModelsConfig(...)` call to accept and pass through `workers`:

```python
def _cfg(cap=50.0, workers=1):
    return config.ModelsConfig(
        "m",
        db.VEC_DIM,
        10,
        0.80,
        config.StageConfig("claude-haiku-4-5", 512),
        config.StageConfig("claude-sonnet-5", 1024, 0.70),
        config.StageConfig("claude-sonnet-5", 1024),
        500,
        0,
        True,
        cap,
        {
            "claude-haiku-4-5": {"input": 0.5, "cached": 0.05, "output": 2.5},
            "claude-sonnet-5": {"input": 1.5, "cached": 0.15, "output": 7.5},
        },
        workers=workers,
    )
```

(This is backward-compatible: every existing call site — `_cfg()` and `_cfg(cap=1.0)` — keeps working unchanged, since `workers` defaults to 1.)

- [ ] **Step 4: Run tests to verify they still fail, now for the right reason**

Run: `uv run pytest tests/triage_verse/test_analyze.py -k parallel -v`
Expected: FAIL — `AttributeError: 'ClaudeCliClient' object has no attribute 'synchronous'`-style errors won't occur (that's already real), but rather the parallel path isn't taken yet, so `client.submit` (not `submit_one`) gets called, which `_ParallelFakeClient` doesn't define: `AttributeError: '_ParallelFakeClient' object has no attribute 'submit'`.

- [ ] **Step 5: Implement `_submit_stage_parallel` and wire it in**

In `src/triage_verse/analyze.py`, the top currently reads:

```python
import json
import time

from . import candidates, classify, db, dedup, prompts, proposals, spend
```

Add `concurrent.futures` (alphabetically first):

```python
import concurrent.futures
import json
import time

from . import candidates, classify, db, dedup, prompts, proposals, spend
```

Modify `_submit_stage` to branch to the parallel path right after the initial log line (insert this 4-line block between `log(f"submitting {stage}: {len(requests)} item(s)")` and the `synchronous = getattr(...)` line — actually, reorder slightly so `synchronous` is computed first since the branch needs it):

```python
def _submit_stage(
    con, cfg, run_id, stage, client, requests, targets, allowed, summary, log
):
    if not requests:
        return True
    log(f"submitting {stage}: {len(requests)} item(s)")
    synchronous = getattr(client, "synchronous", False)
    if synchronous and cfg.workers > 1:
        return _submit_stage_parallel(
            con, cfg, run_id, stage, client, requests, targets, allowed, summary, log
        )
    chunk_size = 1 if synchronous else cfg.max_requests_per_batch
    total = len(requests)
    done = 0
    for start in range(0, total, chunk_size):
        if spend.breaker_tripped(con, cfg):
            log(
                f"budget reached; not submitting more {stage} batches ({done}/{total} done)"
            )
            return False
        chunk = requests[start : start + chunk_size]
        chunk_targets = targets[start : start + chunk_size]
        provider_id = client.submit(chunk)
        batch_id = f"{run_id}:{stage}:{start}"
        db.insert_batch(con, batch_id, run_id, stage, provider_id, len(chunk))
        db.insert_batch_items(
            con, batch_id, {r.custom_id: t for r, t in zip(chunk, chunk_targets)}
        )
        con.commit()
        if synchronous:
            batch_row = con.execute(
                "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if _try_collect_batch(
                con, cfg, run_id, client, allowed, summary, batch_row, log
            ):
                done += len(chunk)
                log(f"  {stage} progress: {done}/{total}")
    return True
```

(Only the `if synchronous and cfg.workers > 1: return _submit_stage_parallel(...)` block is new here — everything else in this function is unchanged from before this task.)

Add the new function right after `_submit_stage` (before `_try_collect_batch`):

```python
def _submit_stage_parallel(
    con, cfg, run_id, stage, client, requests, targets, allowed, summary, log
):
    """Run up to cfg.workers requests concurrently via client.submit_one.

    Worker threads only compute (they call client.submit_one, which touches
    no shared mutable state); this function, running on the caller's own
    thread, owns every database write and log call, so there is exactly one
    writer to `con` at all times -- avoiding SQLite's single-writer
    constraint entirely rather than working around it.

    The budget breaker is checked before dispatching each *new* item, not
    after each completion (money already committed to a running subprocess
    can't be un-spent). That means a tripped budget can be exceeded by at
    most `cfg.workers` already-in-flight items' worth of spend, and a crash
    loses at most that many in-flight items -- both bounds now scale with
    `cfg.workers` instead of being fixed at 1, which is the explicit,
    documented trade-off of running more than one call at a time.
    """
    total = len(requests)
    next_idx = 0
    done = 0
    halted = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        in_flight: dict[concurrent.futures.Future, int] = {}
        while next_idx < total or in_flight:
            while not halted and len(in_flight) < cfg.workers and next_idx < total:
                if spend.breaker_tripped(con, cfg):
                    halted = True
                    break
                future = pool.submit(client.submit_one, requests[next_idx])
                in_flight[future] = next_idx
                next_idx += 1
            if not in_flight:
                break
            done_future = next(concurrent.futures.as_completed(in_flight))
            idx = in_flight.pop(done_future)
            result = done_future.result()
            batch_id = f"{run_id}:{stage}:{idx}"
            db.insert_batch(
                con, batch_id, run_id, stage, f"local:{result.custom_id}", 1
            )
            db.insert_batch_items(con, batch_id, {result.custom_id: targets[idx]})
            _record_and_apply(
                con,
                cfg,
                run_id,
                stage,
                result,
                json.loads(targets[idx]),
                allowed,
                summary,
            )
            db.set_batch(con, batch_id, status="collected", ended_at=db._now())
            con.commit()
            done += 1
            log(f"  {stage} progress: {done}/{total}")
    if halted:
        log(
            f"budget reached; not submitting more {stage} batches ({done}/{total} done)"
        )
        return False
    return True
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_analyze.py -k parallel -v`
Expected: PASS (3 passed).

- [ ] **Step 7: Run the full suite — this is the critical regression check**

Run: `uv run pytest -q`
Expected: PASS, same total count as Task 2's baseline plus the 3 new parallel tests. Specifically confirm:

Run: `git diff tests/triage_verse/test_analyze.py | grep -A5 "def test_analyze_resumes_without_resubmitting"`
Expected: no output (this function must not appear in the diff at all — it uses plain `FakeBatchClient`, which has no `synchronous` attribute, so it never enters the new parallel branch and its behavior must be provably unchanged).

- [ ] **Step 8: Lint/types**

Run: `make py-check`
Expected: ruff + pyright clean.

- [ ] **Step 9: Commit**

```bash
git add src/triage_verse/analyze.py tests/triage_verse/test_analyze.py
git commit -m "feat: run claude_cli calls concurrently with a bounded worker pool"
```

---

### Task 4: Update documentation to describe the widened (but still bounded) guarantee

**Files:**
- Modify: `src/triage_verse/llm.py:189-199` (the `ClaudeCliClient` docstring)
- Modify: `README.md:56-61`

Do **not** touch anything under `docs/superpowers/specs/`, `docs/superpowers/plans/`, or `decisions/` — those are dated historical records per this repo's convention.

- [ ] **Step 1: Update `ClaudeCliClient`'s docstring**

In `src/triage_verse/llm.py`, replace the current docstring:

```python
class ClaudeCliClient:
    """Runs `claude -p` per request on Claude Code's ambient auth (no API key).

    Unlike the Anthropic Batch API client, this backend executes each request
    and incurs its real cost synchronously inside `submit()`, before results
    are ever collected. `analyze._submit_stage` detects this via the
    `synchronous` marker below and chunks at size 1, collecting each item
    immediately after it is submitted -- so `analyze`'s daily-budget breaker
    is checked before every single item, not just once per up-to-500-item
    chunk, and bounds spend *within* a stage as well as between stages/runs.
    """
```

with:

```python
class ClaudeCliClient:
    """Runs `claude -p` per request on Claude Code's ambient auth (no API key).

    Unlike the Anthropic Batch API client, this backend executes each request
    and incurs its real cost synchronously inside `submit()`/`submit_one()`,
    before results are ever collected. `analyze._submit_stage` detects this
    via the `synchronous` marker below. With `cfg.workers == 1` (the
    default), it chunks at size 1 and collects each item immediately after
    it is submitted, so the daily-budget breaker is checked before every
    single item. With `cfg.workers > 1`, up to that many `submit_one` calls
    run concurrently in a bounded worker pool, and the breaker is checked
    before each new dispatch -- bounding a tripped budget's overshoot, and a
    crash's loss, to at most `cfg.workers` items instead of 1. Either way,
    this is enforced *within* a stage, not just between stages/runs.
    """
```

- [ ] **Step 2: Update the README**

In `README.md`, replace the current paragraph:

```markdown
`analyze` is a resumable state machine: re-running it collects in-flight
batches rather than resubmitting, so an interrupted run (or the future
scheduled job) simply continues. Spend is metered to the mirror's `spend`
table and capped by `max_usd_per_day` in `config/models.yaml`. Under
`backend: claude_cli`, each `claude -p` call executes and bills synchronously,
so items are submitted and collected one at a time and `max_usd_per_day` is
checked before every item, bounding spend within a stage as well as between
stages/runs; use `--limit` to additionally bound a single run's spend (each
call costs roughly $0.01-0.02).
```

with:

```markdown
`analyze` is a resumable state machine: re-running it collects in-flight
batches rather than resubmitting, so an interrupted run (or the future
scheduled job) simply continues. Spend is metered to the mirror's `spend`
table and capped by `max_usd_per_day` in `config/models.yaml`. Under
`backend: claude_cli`, each `claude -p` call executes and bills
synchronously; `config/models.yaml`'s `batch.workers` controls how many run
concurrently (default 1, this repo starts at 2). `max_usd_per_day` is
checked before every new dispatch, bounding a tripped budget's overshoot
(and a crash's loss) to at most `workers` items instead of the whole stage;
use `--limit` to additionally bound a single run's spend (each call costs
roughly $0.01-0.02).
```

- [ ] **Step 3: Run the full suite one more time**

Run: `make py-check`
Expected: clean (docs-only changes, but confirms nothing else broke).

- [ ] **Step 4: Commit**

```bash
git add src/triage_verse/llm.py README.md
git commit -m "docs: describe the worker-pool breaker/durability trade-off"
```

---

## Self-Review

**Spec coverage:** worker pool execution model (ThreadPoolExecutor, `submit_one` as the worker primitive, main-thread-only persistence) → Task 3. `workers` config field, defaulting to 1, nested under `batch:` → Task 1. Shared persistence helper avoiding duplicated logic between the sequential and parallel paths → Task 2. Breaker widening to "at most `workers` items" and durability widening to the same bound → Task 3's tests. Non-synchronous clients and `workers=1` provably unaffected → Task 3 Step 7's explicit diff check on `test_analyze_resumes_without_resubmitting`. Doc updates in the three places that previously stated the stricter "at most 1 item" guarantee → Task 4. Rate-limit backoff explicitly out of scope, tracked in issue #24 (not a task here).

**Placeholder scan:** none — every step has concrete, complete code or an exact command with an expected result.

**Type consistency:** `_record_and_apply(con, cfg, run_id, stage, result, target, allowed, summary)` (Task 2) is called identically from `_try_collect_batch` (existing caller, updated in Task 2) and `_submit_stage_parallel` (new caller, Task 3) with the same argument order. `_submit_stage_parallel`'s signature and return contract (`bool`, `True`/`False` for "fully submitted"/"halted on budget") matches `_submit_stage`'s existing contract exactly, since `_submit_stage` returns its result directly. `cfg.workers` (Task 1) is read only in `_submit_stage`'s branch condition (Task 3) and nowhere else. The `_cfg(cap=50.0, workers=1)` test helper change (Task 3) is backward-compatible and used consistently by the 3 new tests.
