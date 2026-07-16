# claude_cli Rate-Limit Halt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect a rate-limit failure from `claude -p` and halt the current analyze stage (breaker-style) instead of burning `submit_one`'s two-attempt content-quality retry budget.

**Architecture:** `_default_runner` is changed to surface the CLI's JSON error envelope on a non-zero exit (today it's discarded). `submit_one` inspects that envelope via a new `_is_rate_limit` predicate and returns a new `BatchResult` status `"rate_limited"` *without* looping. Both synchronous submission paths in `analyze.py` treat that status like the daily-budget breaker: stop dispatching new items, leave the affected issue unstored so it re-queues next run, and let already-in-flight items finish. A new `summary["halted_on_rate_limit"]` flag (checked alongside `halted_on_budget` via an `_is_halted` helper) skips later stages and finalization.

**Tech Stack:** Python 3, `uv`, pytest, `jsonschema`. SQLite mirror. No new dependencies.

## Global Constraints

- **Primary gate:** `make py-check` (ruff format check + lint, pyright, pytest) must pass before any Python change is considered done.
- **Logging convention:** long/multi-stage operations log start/end/halt of every stage with a clear marker (e.g. `stage: <name> - halted on rate limit`). Follow the existing `analyze.py` markers.
- **Scope:** `claude_cli` backend only. The `anthropic_batch` backend never produces `rate_limited` (its provider handles limits server-side), so its code paths must remain unaffected.
- **Do not** add cross-worker sleep/backoff, do not touch the CLI's own retry env vars, and do not change the two-attempt content-quality budget for non-rate-limit failures.
- Run a single test with `uv run pytest <path>::<name>`.

---

### Task 1: `_default_runner` surfaces the error envelope on non-zero exit

**Files:**
- Modify: `src/triage_verse/llm.py` (`_default_runner`, currently lines 155-165)
- Test: `tests/triage_verse/test_llm_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_default_runner(args: list[str], prompt: str) -> str` — now returns `proc.stdout` whenever it is non-empty (even on a non-zero exit); raises `RuntimeError` only on a non-zero exit that produced **no** stdout.

- [ ] **Step 1: Add `import pytest` and `import subprocess` to the test file**

At the top of `tests/triage_verse/test_llm_cli.py`, the imports are currently:

```python
import json

from triage_verse import llm
```

Change to:

```python
import json
import subprocess

import pytest

from triage_verse import llm
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/triage_verse/test_llm_cli.py`:

```python
def test_default_runner_returns_stdout_on_nonzero_exit_with_output(monkeypatch):
    # `claude -p --output-format json` prints its error envelope to stdout and
    # still exits non-zero on a rate limit; that stdout must be surfaced, not
    # discarded, so submit_one can classify the failure.
    import types as _t

    def fake_run(*a, **k):
        return _t.SimpleNamespace(
            returncode=1, stdout='{"is_error": true}', stderr="boom"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert llm._default_runner(["--x"], "p") == '{"is_error": true}'


def test_default_runner_raises_on_nonzero_exit_without_output(monkeypatch):
    import types as _t

    def fake_run(*a, **k):
        return _t.SimpleNamespace(returncode=1, stdout="  ", stderr="crash detail")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="crash detail"):
        llm._default_runner(["--x"], "p")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_llm_cli.py::test_default_runner_returns_stdout_on_nonzero_exit_with_output tests/triage_verse/test_llm_cli.py::test_default_runner_raises_on_nonzero_exit_without_output -v`
Expected: FAIL — the first raises `RuntimeError` today (stdout discarded on non-zero exit).

- [ ] **Step 4: Change `_default_runner`**

In `src/triage_verse/llm.py`, replace:

```python
def _default_runner(args: list[str], prompt: str) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_CLI_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout
```

with:

```python
def _default_runner(args: list[str], prompt: str) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_CLI_TIMEOUT,
    )
    # `claude -p --output-format json` prints its result envelope -- including
    # is_error / api_error_status on a rate limit -- to stdout and still exits
    # non-zero. Surface that stdout so submit_one can classify the failure from
    # the envelope; only a non-zero exit with no stdout is an opaque crash
    # (or timeout) worth raising with its stderr for diagnostics.
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"claude -p exited {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_llm_cli.py -v`
Expected: PASS (all, including the pre-existing tests).

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/llm.py tests/triage_verse/test_llm_cli.py
git commit -m "feat(llm): surface claude -p error envelope on non-zero exit (#24)"
```

---

### Task 2: `_is_rate_limit` predicate

**Files:**
- Modify: `src/triage_verse/llm.py` (add near the other module-level constants, e.g. after `_CLI_TIMEOUT` on line 152)
- Test: `tests/triage_verse/test_llm_cli.py`

**Interfaces:**
- Produces: `_is_rate_limit(envelope: dict) -> bool` and module constant `_RATE_LIMIT_PATTERNS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_llm_cli.py`:

```python
def test_is_rate_limit_detects_api_error_status():
    assert llm._is_rate_limit({"api_error_status": 429}) is True
    assert llm._is_rate_limit({"api_error_status": 529}) is True


def test_is_rate_limit_detects_error_string_patterns():
    for msg in (
        "You've hit your weekly limit · resets Mon 12:00am",
        "API Error: Request rejected (429)",
        "Repeated 529 Overloaded errors",
        "Server is temporarily limiting requests (not your usage limit)",
    ):
        assert llm._is_rate_limit({"is_error": True, "result": msg}) is True, msg


def test_is_rate_limit_ignores_non_rate_limit_and_success():
    assert llm._is_rate_limit({"is_error": True, "result": "billing_error"}) is False
    assert llm._is_rate_limit({"api_error_status": None, "result": "4"}) is False
    assert llm._is_rate_limit({}) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_llm_cli.py -k is_rate_limit -v`
Expected: FAIL with `AttributeError: module 'triage_verse.llm' has no attribute '_is_rate_limit'`.

- [ ] **Step 3: Add the predicate**

In `src/triage_verse/llm.py`, immediately after the `_CLI_TIMEOUT = 300 ...` line (currently line 152), add:

```python
# Substrings (matched case-insensitively against the CLI's error `result`
# string) that mark a rate-limit / usage-limit / overload failure. The exact
# strings vary by CLI version and limit kind (transient 429/529 throttle vs.
# sustained session/weekly/Opus subscription limit), so this is a deliberately
# broad, easily-extended list rather than an exhaustive match. `subtype` is NOT
# keyed on -- its value differs across CLI versions.
_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "429",
    "529",
    "overloaded",
    "usage limit",
    "temporarily limiting requests",
    "hit your",
)


def _is_rate_limit(envelope: dict) -> bool:
    """True if a `claude -p --output-format json` envelope reports a rate limit.

    Robust to CLI-version drift by checking two independent signals: the numeric
    `api_error_status` (429/529), and, for any error envelope, a substring match
    of the human `result` string against `_RATE_LIMIT_PATTERNS`.
    """
    if envelope.get("api_error_status") in (429, 529):
        return True
    if not envelope.get("is_error"):
        return False
    text = str(envelope.get("result") or "").lower()
    return any(pattern in text for pattern in _RATE_LIMIT_PATTERNS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_llm_cli.py -k is_rate_limit -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/llm.py tests/triage_verse/test_llm_cli.py
git commit -m "feat(llm): add _is_rate_limit envelope predicate (#24)"
```

---

### Task 3: `submit_one` returns `rate_limited` without burning the retry budget

**Files:**
- Modify: `src/triage_verse/llm.py` (`BatchResult` status comment on line 33; `submit_one` attempt loop, currently lines 267-313)
- Test: `tests/triage_verse/test_llm_cli.py`

**Interfaces:**
- Consumes: `_is_rate_limit` (Task 2), `_CliMessage`, `_usage_ns` (existing).
- Produces: `ClaudeCliClient.submit_one` may now return `BatchResult(status="rate_limited", message=_CliMessage({}, usage), cost_usd=..., error=<str>)`. On a rate limit it returns on the **first** occurrence without looping; a non-rate-limit error envelope still consumes an attempt and eventually returns `errored`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_llm_cli.py`:

```python
def _error_envelope(result_text, api_error_status=None, cost=0.01):
    return json.dumps(
        {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "api_error_status": api_error_status,
            "result": result_text,
            "total_cost_usd": cost,
            "usage": {"input_tokens": 5, "output_tokens": 0, "cache_read_input_tokens": 0},
        }
    )


def test_submit_one_rate_limited_on_api_error_status_without_burning_budget():
    calls = []

    def runner(args, prompt):
        calls.append(1)
        return _error_envelope("API Error: Request rejected (429)", api_error_status=429)

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "rate_limited"
    assert len(calls) == 1  # did NOT consume the second content-retry attempt
    assert result.cost_usd == 0.01  # spend still tracked
    assert result.usage is not None


def test_submit_one_rate_limited_on_usage_limit_string():
    def runner(args, prompt):
        return _error_envelope("You've hit your weekly limit · resets Mon 12:00am")

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "rate_limited"


def test_submit_one_errors_on_non_rate_limit_error_after_two_attempts():
    calls = []

    def runner(args, prompt):
        calls.append(1)
        return _error_envelope("billing_error: insufficient credits")

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "errored"
    assert len(calls) == 2  # a generic error still burns both attempts
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_llm_cli.py -k submit_one -v`
Expected: FAIL — today a 429 envelope falls into `_extract_json_object`, fails, and burns both attempts, returning `errored`.

- [ ] **Step 3: Update the `BatchResult` status comment**

In `src/triage_verse/llm.py`, change line 33 from:

```python
    status: str  # succeeded | errored | canceled | expired
```

to:

```python
    status: str  # succeeded | errored | rate_limited | canceled | expired
```

- [ ] **Step 4: Insert rate-limit and generic-error handling into the `submit_one` loop**

In `src/triage_verse/llm.py`, the loop body currently reads:

```python
            try:
                envelope = json.loads(self._runner(args, user))
            except Exception as exc:  # noqa: BLE001 - any runner/parse failure is a failed attempt
                last_error = f"cli-call-failed: {exc}"
                continue
            total_cost += float(envelope.get("total_cost_usd") or 0.0)
            last_usage = _usage_ns(envelope.get("usage") or {})
            try:
                data = _extract_json_object(envelope["result"])
                jsonschema.validate(data, schema)
            except (ValueError, json.JSONDecodeError, jsonschema.ValidationError):
                last_error = "cli-output-invalid"
                continue
            return BatchResult(
                request.custom_id,
                "succeeded",
                message=_CliMessage(data, last_usage),
                cost_usd=total_cost,
            )
```

Replace it with (adds the two `if` blocks between `last_usage = ...` and the `try`):

```python
            try:
                envelope = json.loads(self._runner(args, user))
            except Exception as exc:  # noqa: BLE001 - any runner/parse failure is a failed attempt
                last_error = f"cli-call-failed: {exc}"
                continue
            total_cost += float(envelope.get("total_cost_usd") or 0.0)
            last_usage = _usage_ns(envelope.get("usage") or {})
            if _is_rate_limit(envelope):
                # A rate limit is a predictable, load-driven failure, not a
                # content-quality one -- return immediately so it does NOT
                # consume the two-attempt budget. `analyze` halts the stage on
                # this status; the item re-queues next run. Carry usage/cost so
                # tokens already spent stay metered.
                return BatchResult(
                    request.custom_id,
                    "rate_limited",
                    message=_CliMessage({}, last_usage),
                    error=str(envelope.get("result") or "rate-limited"),
                    cost_usd=total_cost,
                )
            if envelope.get("is_error"):
                # A non-rate-limit error (e.g. billing/auth): treat as a failed
                # attempt, matching the pre-existing "burn an attempt then
                # error" behavior, and avoid feeding a null/error `result` into
                # the JSON extractor.
                last_error = f"cli-error: {envelope.get('result') or envelope.get('api_error_status')}"
                continue
            try:
                data = _extract_json_object(envelope["result"])
                jsonschema.validate(data, schema)
            except (ValueError, json.JSONDecodeError, jsonschema.ValidationError):
                last_error = "cli-output-invalid"
                continue
            return BatchResult(
                request.custom_id,
                "succeeded",
                message=_CliMessage(data, last_usage),
                cost_usd=total_cost,
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_llm_cli.py -v`
Expected: PASS (all, including pre-existing tests).

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/llm.py tests/triage_verse/test_llm_cli.py
git commit -m "feat(llm): return rate_limited status without burning retry budget (#24)"
```

---

### Task 4: `analyze` halt wiring + sequential-path detection

**Files:**
- Modify: `src/triage_verse/analyze.py` (`analyze` summary init line 35 and the three `_submit_stage` call sites lines 64-113 & 138-161 & guard lines 88, 123, 182; `_submit_stage` lines 235-281; add module constants + `_is_halted` + `_apply_halt` helpers)
- Test: `tests/triage_verse/test_analyze.py`

**Interfaces:**
- Consumes: `BatchResult.status == "rate_limited"` (Task 3), `spend.record_spend`, `_model`, `db._now` (existing).
- Produces:
  - `_submit_stage(...)` and `_submit_stage_parallel(...)` now return `None` (success/nothing-to-do), `"budget"`, or `"rate_limit"` instead of a bool.
  - `_is_halted(summary) -> bool` returns `summary["halted_on_budget"] or summary["halted_on_rate_limit"]`.
  - `_apply_halt(summary, halt) -> None` sets the matching summary flag.
  - `summary` now contains `"halted_on_rate_limit": bool`.
  - Module constant `_HALT_LOG = {None: "done", "budget": "halted on budget", "rate_limit": "halted on rate limit"}`.

- [ ] **Step 1: Write the failing test (sequential path, `workers == 1`)**

Append to `tests/triage_verse/test_analyze.py`:

```python
def test_rate_limited_result_halts_sequential_stage_and_requeues(tmp_path):
    # A rate limit on the 2nd classify item halts the stage: the 1st item is
    # classified, the 3rd is never submitted, and the halt is reported without
    # writing proposals (the run is left to resume next invocation).
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 3)
    scripted = {
        "c0": {"status": "succeeded", "payload": _clf(0.9)},
        "c1": {"status": "rate_limited"},
        "c2": {"status": "succeeded", "payload": _clf(0.9)},
    }
    client = _SyncFakeClient(scripted)
    summary = analyze.analyze(
        con,
        _cfg(),
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )
    assert summary["halted_on_rate_limit"] is True
    assert summary["classified"] == 1
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1
    # finalization skipped -> no proposals dir created
    assert not (tmp_path / "proposals").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_analyze.py::test_rate_limited_result_halts_sequential_stage_and_requeues -v`
Expected: FAIL with `KeyError: 'halted_on_rate_limit'` (summary lacks the key today).

- [ ] **Step 3: Add module constants and helpers**

In `src/triage_verse/analyze.py`, immediately above `def _submit_stage(` (currently line 235), add:

```python
# _submit_stage / _submit_stage_parallel return one of these. None means the
# stage completed (or had nothing to do); the strings mark why dispatch stopped.
_HALT_LOG = {
    None: "done",
    "budget": "halted on budget",
    "rate_limit": "halted on rate limit",
}


def _is_halted(summary) -> bool:
    return summary["halted_on_budget"] or summary["halted_on_rate_limit"]


def _apply_halt(summary, halt) -> None:
    if halt == "budget":
        summary["halted_on_budget"] = True
    elif halt == "rate_limit":
        summary["halted_on_rate_limit"] = True
```

- [ ] **Step 4: Add the new summary key**

In `src/triage_verse/analyze.py` line 35, change:

```python
    summary = {"classified": 0, "rechecked": 0, "pairs": 0, "halted_on_budget": False}
```

to:

```python
    summary = {
        "classified": 0,
        "rechecked": 0,
        "pairs": 0,
        "halted_on_budget": False,
        "halted_on_rate_limit": False,
    }
```

- [ ] **Step 5: Update the classify call site (lines 64-87)**

Change:

```python
    if not _stage_started(con, run_id, "classify"):
        if (
            _submit_stage(
                con,
                cfg,
                run_id,
                "classify",
                batch_client,
                classify.build_requests(
                    con,
                    cfg,
                    cfg.classify,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    issues,
                    prefix="c",
                ),
                targets=[json.dumps([i["repo"], i["number"]]) for i in issues],
                allowed=allowed,
                summary=summary,
                log=log,
            )
            is False
        ):
            summary["halted_on_budget"] = True
```

to:

```python
    if not _stage_started(con, run_id, "classify"):
        _apply_halt(
            summary,
            _submit_stage(
                con,
                cfg,
                run_id,
                "classify",
                batch_client,
                classify.build_requests(
                    con,
                    cfg,
                    cfg.classify,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    issues,
                    prefix="c",
                ),
                targets=[json.dumps([i["repo"], i["number"]]) for i in issues],
                allowed=allowed,
                summary=summary,
                log=log,
            ),
        )
```

- [ ] **Step 6: Update the dedup call site (lines 88-113)**

Change:

```python
    if not summary["halted_on_budget"] and not _stage_started(con, run_id, "dedup"):
        if (
            _submit_stage(
                con,
                cfg,
                run_id,
                "dedup",
                batch_client,
                dedup.build_requests(
                    con,
                    cfg.dedup,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    pairs,
                    prefix="d",
                ),
                targets=[
                    json.dumps([[a[0], a[1], a[2]], [b[0], b[1], b[2]]])
                    for a, b in pairs
                ],
                allowed=allowed,
                summary=summary,
                log=log,
            )
            is False
        ):
            summary["halted_on_budget"] = True
```

to:

```python
    if not _is_halted(summary) and not _stage_started(con, run_id, "dedup"):
        _apply_halt(
            summary,
            _submit_stage(
                con,
                cfg,
                run_id,
                "dedup",
                batch_client,
                dedup.build_requests(
                    con,
                    cfg.dedup,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    pairs,
                    prefix="d",
                ),
                targets=[
                    json.dumps([[a[0], a[1], a[2]], [b[0], b[1], b[2]]])
                    for a, b in pairs
                ],
                allowed=allowed,
                summary=summary,
                log=log,
            ),
        )
```

- [ ] **Step 7: Update the recheck guard and call site (lines 123, 138-161)**

In `maybe_recheck`, change the guard:

```python
        if summary["halted_on_budget"]:
            return
```

to:

```python
        if _is_halted(summary):
            return
```

and change the recheck submission:

```python
        if (
            _submit_stage(
                con,
                cfg,
                run_id,
                "recheck",
                batch_client,
                classify.build_requests(
                    con,
                    cfg,
                    cfg.recheck,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    to_recheck,
                    prefix="r",
                    with_comments=True,
                ),
                targets=[json.dumps([i["repo"], i["number"]]) for i in to_recheck],
                allowed=allowed,
                summary=summary,
                log=log,
            )
            is False
        ):
            summary["halted_on_budget"] = True
```

to:

```python
        _apply_halt(
            summary,
            _submit_stage(
                con,
                cfg,
                run_id,
                "recheck",
                batch_client,
                classify.build_requests(
                    con,
                    cfg,
                    cfg.recheck,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    to_recheck,
                    prefix="r",
                    with_comments=True,
                ),
                targets=[json.dumps([i["repo"], i["number"]]) for i in to_recheck],
                allowed=allowed,
                summary=summary,
                log=log,
            ),
        )
```

- [ ] **Step 8: Update the finalization guard (line 182)**

Change:

```python
    if not db.open_batches(con) and not summary["halted_on_budget"]:
```

to:

```python
    if not db.open_batches(con) and not _is_halted(summary):
```

- [ ] **Step 9: Rewrite `_submit_stage` to return the halt reason and detect rate-limit in the sequential path**

Replace the whole body of `_submit_stage` (currently lines 235-281) with:

```python
def _submit_stage(
    con, cfg, run_id, stage, client, requests, targets, allowed, summary, log
):
    if not requests:
        log(f"stage: {stage} - skipped (nothing to do)")
        return None
    log(f"stage: {stage} - starting ({len(requests)} item(s))")
    synchronous = getattr(client, "synchronous", False)
    if synchronous and cfg.workers > 1:
        halt = _submit_stage_parallel(
            con, cfg, run_id, stage, client, requests, targets, allowed, summary, log
        )
        log(f"stage: {stage} - {_HALT_LOG[halt]}")
        return halt
    chunk_size = 1 if synchronous else cfg.max_requests_per_batch
    total = len(requests)
    done = 0
    for start in range(0, total, chunk_size):
        if spend.breaker_tripped(con, cfg):
            log(
                f"budget reached; not submitting more {stage} batches ({done}/{total} done)"
            )
            log(f"stage: {stage} - halted on budget")
            return "budget"
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
            collected = _try_collect_batch(
                con, cfg, run_id, client, allowed, summary, batch_row, log
            )
            rate_limited = any(
                r.status == "rate_limited" for r in client.results(provider_id)
            )
            if collected and not rate_limited:
                done += len(chunk)
                log(f"  {stage} progress: {done}/{total}")
            if rate_limited:
                # A sustained rate limit: stop submitting more items this run.
                # The rate-limited item stored no proposal (parse returns None
                # for non-succeeded), so it re-queues on the next run.
                log(
                    f"rate limit reached; not submitting more {stage} batches "
                    f"({done}/{total} done)"
                )
                log(f"stage: {stage} - halted on rate limit")
                return "rate_limit"
    if synchronous:
        log(f"stage: {stage} - done")
    else:
        log(f"stage: {stage} - submitted, awaiting async completion")
    return None
```

- [ ] **Step 10: Run the sequential halt test to verify it passes**

Run: `uv run pytest tests/triage_verse/test_analyze.py::test_rate_limited_result_halts_sequential_stage_and_requeues -v`
Expected: PASS.

- [ ] **Step 11: Run the full analyze + llm suites (regression check for the bool→string return change)**

Run: `uv run pytest tests/triage_verse/test_analyze.py tests/triage_verse/test_llm_cli.py -v`
Expected: PASS. (`_submit_stage_parallel` is rewritten in Task 5; it still returns a truthy/`None` value that `_apply_halt` tolerates — `_apply_halt(summary, True)` and `_apply_halt(summary, False)` are both no-ops until then, so the existing parallel budget test on line ~338 may now assert `halted_on_budget is True` and fail. If it fails here, that is expected and is fixed by Task 5 — proceed.)

> Note: the existing `_submit_stage_parallel` still returns `False`/`True` at this point. `_apply_halt(summary, False)` leaves both flags `False`, so the parallel budget-halt test (`test_..._budget...`, line ~338) will fail until Task 5 makes the parallel path return `"budget"`. This is the one intentional cross-task dependency.

- [ ] **Step 12: Commit**

```bash
git add src/triage_verse/analyze.py tests/triage_verse/test_analyze.py
git commit -m "feat(analyze): halt stage on rate_limited result, sequential path (#24)"
```

---

### Task 5: parallel-path rate-limit halt + budget-halt return migration

**Files:**
- Modify: `src/triage_verse/analyze.py` (`_submit_stage_parallel`, currently lines 284-346)
- Test: `tests/triage_verse/test_analyze.py`

**Interfaces:**
- Consumes: `BatchResult.status == "rate_limited"`, `spend.record_spend`, `_model`, `db._now`.
- Produces: `_submit_stage_parallel(...)` now returns `None` (success), `"budget"`, or `"rate_limit"` (previously `True`/`False`). On a `rate_limited` result it records that item's spend, skips its batch row (leaving it unstored so it re-queues), stops dispatching new items, drains and records already-in-flight items, and returns `"rate_limit"`.

- [ ] **Step 1: Write the failing test (parallel path, `workers == 2`)**

Append to `tests/triage_verse/test_analyze.py`:

```python
class _RateLimitedParallelClient:
    """submit_one-only client: one custom_id returns rate_limited (instantly),
    every other item succeeds after a small delay so the rate-limited item wins
    the race and halts dispatch deterministically while an in-flight item is
    still running (proving in-flight items are drained and recorded)."""

    synchronous = True

    def __init__(self, rate_limited_cid, delay=0.1):
        self.rate_limited_cid = rate_limited_cid
        self.delay = delay
        self._lock = threading.Lock()
        self.submitted = []

    def submit_one(self, request):
        with self._lock:
            self.submitted.append(request.custom_id)
        if request.custom_id == self.rate_limited_cid:
            return llm.BatchResult(request.custom_id, "rate_limited", cost_usd=0.0)
        time.sleep(self.delay)
        return llm.BatchResult(request.custom_id, "succeeded", message=_Msg(_clf(0.9)))


def test_parallel_stage_halts_on_rate_limit_and_drains_inflight(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 4)
    client = _RateLimitedParallelClient(rate_limited_cid="c0")
    summary = analyze.analyze(
        con,
        _cfg(workers=2),
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )
    assert summary["halted_on_rate_limit"] is True
    # workers=2 fills [c0, c1]; c0 rate-limits instantly and halts before c2/c3
    # are ever dispatched, but the in-flight c1 still completes and is recorded.
    assert client.submitted == ["c0", "c1"]
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_analyze.py::test_parallel_stage_halts_on_rate_limit_and_drains_inflight -v`
Expected: FAIL — today `_submit_stage_parallel` records the `rate_limited` result as a normal batch and never halts, so `halted_on_rate_limit` stays `False`.

- [ ] **Step 3: Rewrite `_submit_stage_parallel`**

Replace the whole body of `_submit_stage_parallel` (currently lines 284-346) with (docstring preserved and extended):

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

    A `rate_limited` result halts new dispatch the same way, but items already
    in flight are still drained and recorded (their spend is committed and they
    may still succeed). The rate-limited item's own spend is recorded, but no
    batch row is written for it, so it re-queues on the next run.

    Returns None on success, or "budget" / "rate_limit" on a halt.
    """
    total = len(requests)
    next_idx = 0
    done = 0
    halt = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        in_flight: dict[concurrent.futures.Future, int] = {}
        while next_idx < total or in_flight:
            while halt is None and len(in_flight) < cfg.workers and next_idx < total:
                if spend.breaker_tripped(con, cfg):
                    halt = "budget"
                    break
                future = pool.submit(client.submit_one, requests[next_idx])
                in_flight[future] = next_idx
                next_idx += 1
            if not in_flight:
                break
            done_future = next(concurrent.futures.as_completed(in_flight))
            idx = in_flight.pop(done_future)
            result = done_future.result()
            if result.status == "rate_limited":
                # Record spend so the daily total stays accurate, but write no
                # batch row -- the item stays unstored and re-queues next run.
                # Stop dispatching new items; in-flight ones still drain below.
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
                    con.commit()
                halt = "rate_limit"
                continue
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
    if halt == "budget":
        log(
            f"budget reached; not submitting more {stage} batches ({done}/{total} done)"
        )
    elif halt == "rate_limit":
        log(
            f"rate limit reached; not submitting more {stage} batches "
            f"({done}/{total} done)"
        )
    return halt
```

- [ ] **Step 4: Run the parallel test to verify it passes**

Run: `uv run pytest tests/triage_verse/test_analyze.py::test_parallel_stage_halts_on_rate_limit_and_drains_inflight -v`
Expected: PASS.

- [ ] **Step 5: Run the full analyze suite (confirms the budget-halt parallel test is green again)**

Run: `uv run pytest tests/triage_verse/test_analyze.py -v`
Expected: PASS — including the pre-existing parallel budget-halt test (`_submit_stage_parallel` now returns `"budget"`, which `_apply_halt` maps to `summary["halted_on_budget"] = True`).

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/analyze.py tests/triage_verse/test_analyze.py
git commit -m "feat(analyze): halt parallel pool on rate_limited, drain in-flight (#24)"
```

---

### Task 6: surface the rate-limit halt in the CLI summary

**Files:**
- Modify: `src/triage_verse/cli.py` (`_cmd_analyze` human string, lines 192-195)
- Modify: `tests/triage_verse/test_cli_analyze.py` (two fake summaries: lines 17 and 98-103)

**Interfaces:**
- Consumes: `summary["halted_on_rate_limit"]` (Task 4).
- Produces: the human-readable analyze summary now includes `halted_on_rate_limit=<bool>`.

- [ ] **Step 1: Update the two fake summaries so they carry the new key**

In `tests/triage_verse/test_cli_analyze.py`, change line 17 from:

```python
        return {"classified": 3, "rechecked": 1, "pairs": 2, "halted_on_budget": False}
```

to:

```python
        return {
            "classified": 3,
            "rechecked": 1,
            "pairs": 2,
            "halted_on_budget": False,
            "halted_on_rate_limit": False,
        }
```

and change the second fake (lines 98-103) from:

```python
        lambda con, cfg, **kw: {
            "classified": 0,
            "rechecked": 0,
            "pairs": 0,
            "halted_on_budget": False,
        },
```

to:

```python
        lambda con, cfg, **kw: {
            "classified": 0,
            "rechecked": 0,
            "pairs": 0,
            "halted_on_budget": False,
            "halted_on_rate_limit": False,
        },
```

- [ ] **Step 2: Add a failing assertion on the human summary string**

In `tests/triage_verse/test_cli_analyze.py`, inside `test_cli_analyze_invokes_pipeline`, after `assert rc == 0` (line 41), add:

```python
    out = capsys.readouterr().out
    assert "halted_on_rate_limit=False" in out
```

and add `capsys` to that test's signature — change:

```python
def test_cli_analyze_invokes_pipeline(tmp_path, monkeypatch):
```

to:

```python
def test_cli_analyze_invokes_pipeline(tmp_path, monkeypatch, capsys):
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_cli_analyze.py::test_cli_analyze_invokes_pipeline -v`
Expected: FAIL — the summary string does not yet include `halted_on_rate_limit`.

- [ ] **Step 4: Update the `_cmd_analyze` human string**

In `src/triage_verse/cli.py`, change (lines 192-195):

```python
    human = (
        f"classified={summary['classified']} rechecked={summary['rechecked']} "
        f"pairs={summary['pairs']} halted_on_budget={summary['halted_on_budget']}"
    )
```

to:

```python
    human = (
        f"classified={summary['classified']} rechecked={summary['rechecked']} "
        f"pairs={summary['pairs']} halted_on_budget={summary['halted_on_budget']} "
        f"halted_on_rate_limit={summary['halted_on_rate_limit']}"
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/triage_verse/test_cli_analyze.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_analyze.py
git commit -m "feat(cli): report halted_on_rate_limit in analyze summary (#24)"
```

---

### Task 7: full gate + issue reference

**Files:** none (verification only)

- [ ] **Step 1: Run the primary Python gate**

Run: `make py-check`
Expected: PASS — ruff format check, ruff lint, pyright, and the full pytest suite all green. If ruff reformats, re-run and re-commit the formatting under the nearest relevant task's scope.

- [ ] **Step 2: Sanity-check the diff against the spec**

Run: `git diff origin/main... --stat`
Expected: changes confined to `src/triage_verse/llm.py`, `src/triage_verse/analyze.py`, `src/triage_verse/cli.py`, their tests, and the two `docs/superpowers/` documents. No changes to `anthropic_batch` code paths, config, or the egress guard.

- [ ] **Step 3: Confirm no stray `is False` / bool assumptions remain on the stage return**

Run: `grep -n "halted_on_budget\|_submit_stage\|is False" src/triage_verse/analyze.py`
Expected: every `_submit_stage(...)` result flows through `_apply_halt`; no remaining `is False` comparison on a stage return; guards use `_is_halted(summary)`.

---

## Self-Review

**Spec coverage:**
- Surface envelope on non-zero exit → Task 1. ✓
- `_is_rate_limit` multi-signal predicate (api_error_status + string patterns, not `subtype`) → Task 2. ✓
- New `rate_limited` status returned without burning the 2-attempt budget; non-rate-limit errors keep today's behavior → Task 3. ✓
- Halt propagation: `summary["halted_on_rate_limit"]`, `_is_halted`, gating of dedup/recheck/finalization → Task 4. ✓
- Sequential-path detection → Task 4; parallel-path detection with in-flight drain + spend recording + no batch row → Task 5. ✓
- Logging with a distinct rate-limit marker → Tasks 4 & 5 (`stage: <name> - halted on rate limit`, `rate limit reached; ...`). ✓
- CLI summary reporting → Task 6. ✓
- Scope guard (anthropic_batch untouched; content-budget unchanged for non-rate-limit) → Task 7 verification. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type consistency:** `_submit_stage` and `_submit_stage_parallel` both return `None | "budget" | "rate_limit"`; `_apply_halt`/`_is_halted`/`_HALT_LOG` all use those exact strings; `BatchResult(status="rate_limited", ...)` used consistently across Tasks 3, 5, and both tests. The one intentional cross-task dependency (Task 4 changes the call-site contract before Task 5 migrates the parallel return) is called out explicitly in Task 4 Step 11. ✓
