# `claude -p` Model Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a config-selected `ClaudeCliClient` model backend that drives `claude -p` on Claude Code's ambient auth, so the analysis pipeline runs with no `ANTHROPIC_API_KEY`.

**Architecture:** `ClaudeCliClient` implements the existing `BatchClient` interface (`submit`/`status`/`results`), running `claude -p --output-format json` once per request (sequentially), parsing/validating/retrying the output, and reporting cost from the CLI's `total_cost_usd`. A `backend` config field + `make_batch_client(cfg)` factory selects it vs `AnthropicBatchClient`. The `analyze` state machine and all stages are unchanged.

**Tech Stack:** Python ≥3.11, `uv`; the `claude` CLI (Claude Code); `jsonschema` for output validation; `pytest`; `ruff` + `pyright`.

## Global Constraints

- Python floor `>=3.11`; all code passes `make py-check` (ruff format+lint, pyright, pytest).
- Tests run with **no network and no real `claude` invocation**: inject a fake `runner` into `ClaudeCliClient`.
- `ClaudeCliClient` must implement the existing `llm.BatchClient` interface exactly (`submit(requests) -> str`, `status(pid) -> str`, `results(pid) -> list[BatchResult]`) so `analyze` needs no changes.
- **Injection safety:** the `claude -p` command disables tools with `--tools ""` placed **last** in the arg list (verified: with tools disabled the model can only emit text — `num_turns` stays 1, nothing executes). Output is schema-validated; a derailed/injected response fails validation and is skipped.
- `--system-prompt` (replace, not append) carries our rubric + schema instructions.
- Model id → CLI alias mapping: `claude-haiku-4-5` → `haiku`, `claude-sonnet-4-6` → `sonnet` (unknown ids pass through unchanged).
- Backends are config-selected via `config/models.yaml` `backend: claude_cli | anthropic_batch`, default `claude_cli`.
- Spend for the CLI backend comes from the reported `total_cost_usd` (both attempts of a retried call are summed and metered).
- Conventional-commit prefixes: `feat:` / `test:` / `chore:` / `docs:`.

**Design reference:** `docs/superpowers/specs/2026-06-29-claude-cli-backend-design.md`. Related issues: #18 (adopt Batch API when a key lands), #19 (parallelize).

## File Structure

- Modify `src/triage_verse/config.py` — add `backend` to `ModelsConfig` + `load_models_config`.
- Modify `src/triage_verse/llm.py` — add `BatchResult.cost_usd`, `ClaudeCliClient`, `make_batch_client`.
- Modify `src/triage_verse/spend.py` — `record_spend` accepts `cost_usd`.
- Modify `src/triage_verse/analyze.py` — pass `result.cost_usd` to `record_spend`.
- Modify `src/triage_verse/cli.py` — `_cmd_analyze` uses `make_batch_client(cfg)`.
- Modify `config/models.yaml`, `pyproject.toml`, `README.md`, and the smoke-notes runbook.
- Tests: `tests/triage_verse/test_llm_cli.py` (new); extend `test_models_config.py`, `test_spend.py`.

---

### Task 1: `backend` config field

**Files:**
- Modify: `src/triage_verse/config.py`
- Modify: `config/models.yaml`
- Test: `tests/triage_verse/test_models_config.py`

**Interfaces:**
- Produces: `ModelsConfig.backend: str` (default `"claude_cli"`); `load_models_config` reads top-level `backend`.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_models_config.py`:

```python
def test_backend_defaults_to_claude_cli(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "embedding: {model: m, dim: 8, candidate_top_k: 3, cosine_threshold: 0.5}\n"
        "stages:\n"
        "  classify: {model: claude-haiku-4-5, max_tokens: 100}\n"
        "  recheck: {model: claude-sonnet-4-6, max_tokens: 200, confidence_floor: 0.6}\n"
        "  dedup: {model: claude-sonnet-4-6, max_tokens: 200}\n"
        "batch: {max_requests_per_batch: 50, poll_interval_seconds: 5}\n"
        "spend: {batch_only: true, max_usd_per_day: 1, pricing: {}}\n"
    )
    assert load_models_config(p).backend == "claude_cli"


def test_backend_read_from_file():
    cfg = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert cfg.backend in {"claude_cli", "anthropic_batch"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_models_config.py -v`
Expected: FAIL (`AttributeError: 'ModelsConfig' object has no attribute 'backend'`).

- [ ] **Step 3: Implement**

In `config.py`, add a trailing field to `ModelsConfig` (a default keeps existing positional constructions in other tests working):

```python
    pricing: dict[str, dict[str, float]]
    backend: str = "claude_cli"
```

In `load_models_config`, pass it through (read the top-level key with a default):

```python
        pricing=sp["pricing"],
        backend=data.get("backend", "claude_cli"),
    )
```

In `config/models.yaml`, add a top-level line (e.g. above `embedding:`):

```yaml
backend: claude_cli   # claude_cli (uses `claude -p`, no API key) | anthropic_batch (needs ANTHROPIC_API_KEY)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_models_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/config.py config/models.yaml tests/triage_verse/test_models_config.py
git commit -m "feat: add backend selector to models config"
```

---

### Task 2: `BatchResult.cost_usd` + `record_spend` uses it

**Files:**
- Modify: `src/triage_verse/llm.py`
- Modify: `src/triage_verse/spend.py`
- Modify: `src/triage_verse/analyze.py`
- Test: `tests/triage_verse/test_spend.py`

**Interfaces:**
- Produces: `llm.BatchResult.cost_usd: float | None = None`; `spend.record_spend(con, run_id, stage, model, pricing, usage, cost_usd=None) -> float` (uses `cost_usd` when not None, else computes from pricing).
- Consumes (unchanged): `db.insert_spend`, `spend.usd_for_usage`.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_spend.py`:

```python
def test_record_spend_prefers_explicit_cost_usd(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    # pricing would compute 0.50, but explicit cost_usd wins
    usd = spend.record_spend(con, "run1", "classify", "claude-haiku-4-5", PRICING,
                             _Usage(1_000_000, 0, 0), cost_usd=0.0188)
    assert usd == 0.0188
    row = con.execute("SELECT usd, input_tokens FROM spend").fetchone()
    assert row["usd"] == 0.0188 and row["input_tokens"] == 1_000_000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_spend.py -v`
Expected: FAIL (`TypeError: record_spend() got an unexpected keyword argument 'cost_usd'`).

- [ ] **Step 3: Implement**

In `llm.py`, add the field to `BatchResult`:

```python
@dataclass
class BatchResult:
    custom_id: str
    status: str
    message: Any = None
    error: Any = None
    cost_usd: float | None = None
```

In `spend.py`, extend `record_spend`:

```python
def record_spend(con, run_id, stage, model, pricing, usage, cost_usd=None) -> float:
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    cached_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    if cost_usd is None:
        cost_usd = usd_for_usage(pricing, model, input_tokens=input_tokens,
                                 cached_tokens=cached_tokens, output_tokens=output_tokens)
    db.insert_spend(con, run_id, stage, model, input_tokens, cached_tokens,
                    output_tokens, cost_usd)
    return cost_usd
```

In `analyze.py`, find the `spend.record_spend(...)` call inside `_collect` and pass the result's cost through (existing backends leave it `None`, so behavior is unchanged for them):

```python
                spend.record_spend(con, run_id, batch["stage"],
                                   _model(cfg, batch["stage"]), cfg.pricing,
                                   result.usage, cost_usd=result.cost_usd)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_spend.py tests/triage_verse/test_analyze.py -v`
Expected: PASS (existing analyze tests still green — `FakeBatchClient` results have `cost_usd=None`, so pricing is still used).

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/llm.py src/triage_verse/spend.py src/triage_verse/analyze.py tests/triage_verse/test_spend.py
git commit -m "feat: let spend metering use a reported cost_usd"
```

---

### Task 3: `ClaudeCliClient`

**Files:**
- Modify: `src/triage_verse/llm.py`
- Modify: `pyproject.toml` (add `jsonschema`)
- Test: `tests/triage_verse/test_llm_cli.py`

**Interfaces:**
- Consumes: `llm.BatchRequest` (its `params` carries `model`, `system` (list of text blocks), `messages` (user content), and `output_config.format.schema`); `llm.BatchResult`.
- Produces: `llm.ClaudeCliClient(runner=<callable>, aliases=...)` implementing `BatchClient`. Each request → a `claude -p` call; results carry a synthetic message (so `extract_json`/`classify.parse`/`dedup.parse` read them unchanged), a usage namespace, and `cost_usd`.

**Verify-first note (security-critical):** the command disables tools with `--tools ""` placed last. Confirm with one probe that a tool-requesting prompt does not actually execute (`num_turns == 1`, no real tool result) before trusting it; a unit test also asserts the built command ends with `--tools ""`.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml` `[project].dependencies`, add `"jsonschema>=4.0"`. Run `uv sync`.

- [ ] **Step 2: Write the failing test**

```python
# tests/triage_verse/test_llm_cli.py
import json

from triage_verse import llm

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["duplicate", "distinct"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}


def _request(cid="c0", model="claude-haiku-4-5"):
    return llm.BatchRequest(cid, {
        "model": model,
        "system": [{"type": "text", "text": "RUBRIC", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "<ISSUE_TITLE>\nx\n</ISSUE_TITLE>"}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    })


def _envelope(result_text, cost=0.01):
    return json.dumps({
        "type": "result", "result": result_text, "total_cost_usd": cost,
        "usage": {"input_tokens": 5, "output_tokens": 10, "cache_read_input_tokens": 0},
    })


def test_parses_fenced_json_and_maps_model():
    calls = []

    def runner(args, prompt):
        calls.append((args, prompt))
        return _envelope('```json\n{"verdict": "duplicate"}\n```', cost=0.02)

    client = llm.ClaudeCliClient(runner=runner)
    pid = client.submit([_request()])
    assert client.status(pid) == "ended"
    (result,) = client.results(pid)
    assert result.status == "succeeded"
    assert result.cost_usd == 0.02
    assert llm.extract_json(result.message) == {"verdict": "duplicate"}
    # command disables tools (last) and selects the haiku alias, json output
    args = calls[0][0]
    assert args[-2:] == ["--tools", ""]
    assert "--output-format" in args and "json" in args
    assert "haiku" in args


def test_retries_once_on_schema_violation_then_succeeds():
    envs = iter([_envelope('{"verdict": "MAYBE"}'), _envelope('{"verdict": "distinct"}')])

    def runner(args, prompt):
        return next(envs)

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "succeeded"
    assert llm.extract_json(result.message) == {"verdict": "distinct"}


def test_errored_after_two_bad_outputs_and_sums_cost():
    def runner(args, prompt):
        return _envelope("not json at all", cost=0.03)

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "errored"
    assert result.cost_usd == 0.06  # both attempts metered


def test_make_batch_client_selects_impl(monkeypatch):
    # AnthropicBatchClient() constructs anthropic.Anthropic(), which requires a
    # key to be present (no network call); set a dummy one so the test is offline.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(llm.make_batch_client(_cfg("claude_cli")), llm.ClaudeCliClient)
    assert isinstance(llm.make_batch_client(_cfg("anthropic_batch")), llm.AnthropicBatchClient)


def _cfg(backend):
    from triage_verse import config
    return config.ModelsConfig("m", 8, 10, 0.8,
        config.StageConfig("claude-haiku-4-5", 512),
        config.StageConfig("claude-sonnet-4-6", 1024, 0.7),
        config.StageConfig("claude-sonnet-4-6", 1024),
        500, 30, True, 50, {}, backend=backend)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_llm_cli.py -v`
Expected: FAIL (`AttributeError: module 'triage_verse.llm' has no attribute 'ClaudeCliClient'`).

- [ ] **Step 4: Implement `ClaudeCliClient` in `llm.py`**

Add imports at the top (`subprocess`, `types`, `jsonschema`) and:

```python
_MODEL_ALIASES = {"claude-haiku-4-5": "haiku", "claude-sonnet-4-6": "sonnet"}
_MAX_PROMPT_CHARS = 50_000


def _default_runner(args: list[str], prompt: str) -> str:
    proc = subprocess.run(["claude", "-p", prompt, *args],
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout


def _extract_json_object(text: str) -> dict:
    t = text.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j > i:
            return json.loads(t[i : j + 1])
        raise ValueError("no JSON object in output")


class _CliBlock:
    type = "text"
    def __init__(self, text: str) -> None:
        self.text = text


class _CliMessage:
    def __init__(self, data: dict, usage: object) -> None:
        self.content = [_CliBlock(json.dumps(data))]
        self.usage = usage


class ClaudeCliClient:
    """Runs `claude -p` per request on Claude Code's ambient auth (no API key)."""

    def __init__(self, runner=_default_runner, aliases=_MODEL_ALIASES) -> None:
        self._runner = runner
        self._aliases = aliases
        self._batches: dict[str, list[BatchResult]] = {}

    def submit(self, requests: list[BatchRequest]) -> str:
        pid = "cli-" + uuid.uuid4().hex[:8]
        self._batches[pid] = [self.submit_one(r) for r in requests]
        return pid

    def status(self, provider_id: str) -> str:
        return "ended"

    def results(self, provider_id: str) -> list[BatchResult]:
        return self._batches[provider_id]

    def submit_one(self, request: BatchRequest) -> BatchResult:
        params = request.params
        model = self._aliases.get(params["model"], params["model"])
        schema = params["output_config"]["format"]["schema"]
        system = "\n".join(b["text"] for b in params["system"])
        user = str(params["messages"][0]["content"])[:_MAX_PROMPT_CHARS]
        total_cost = 0.0
        last_usage: object = types.SimpleNamespace(
            input_tokens=0, cache_read_input_tokens=0, output_tokens=0)
        for attempt in range(2):
            nudge = "" if attempt == 0 else \
                "\nReturn ONLY the JSON object, with no prose and no code fences."
            sys_prompt = (
                system
                + "\n\nRespond ONLY with a JSON object matching this schema:\n"
                + json.dumps(schema) + nudge
            )
            args = ["--model", model, "--output-format", "json",
                    "--system-prompt", sys_prompt, "--tools", ""]
            envelope = json.loads(self._runner(args, user))
            total_cost += float(envelope.get("total_cost_usd") or 0.0)
            last_usage = _usage_ns(envelope.get("usage") or {})
            try:
                data = _extract_json_object(envelope["result"])
                jsonschema.validate(data, schema)
            except (ValueError, json.JSONDecodeError, jsonschema.ValidationError):
                continue
            return BatchResult(request.custom_id, "succeeded",
                               message=_CliMessage(data, last_usage), cost_usd=total_cost)
        return BatchResult(request.custom_id, "errored",
                           error="cli-output-invalid", cost_usd=total_cost)


def _usage_ns(usage: dict) -> object:
    return types.SimpleNamespace(
        input_tokens=usage.get("input_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )


def make_batch_client(cfg) -> BatchClient:
    if cfg.backend == "anthropic_batch":
        return AnthropicBatchClient()
    return ClaudeCliClient()
```

- [ ] **Step 5: Verify the security flag (probe once)**

Run: `claude -p "Run the bash command: echo X" --model haiku --output-format json --tools "" | python3 -c "import sys,json;d=json.load(sys.stdin);print('turns',d['num_turns'])"`
Expected: `turns 1` and no real execution (the model can only emit text). If a real tool round-trip occurs, switch the disabling flag to `--disallowed-tools <Bash Write Edit WebFetch ...>` and update the test's assertion accordingly.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_llm_cli.py -v && make py-check`
Expected: PASS, ruff + pyright clean.

- [ ] **Step 7: Commit**

```bash
git add src/triage_verse/llm.py pyproject.toml uv.lock tests/triage_verse/test_llm_cli.py
git commit -m "feat: add ClaudeCliClient backend driving claude -p"
```

---

### Task 4: Wire the backend factory into the CLI

**Files:**
- Modify: `src/triage_verse/cli.py`
- Test: `tests/triage_verse/test_cli_analyze.py`

**Interfaces:**
- Consumes: `llm.make_batch_client(cfg)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_cli_analyze.py`:

```python
def test_cli_analyze_uses_backend_factory(tmp_path, monkeypatch):
    from triage_verse import analyze as analyze_mod
    from triage_verse import embed as embed_mod
    from triage_verse import llm
    made = {}
    monkeypatch.setattr(embed_mod, "FastEmbedEmbedder", lambda *a, **k: object())
    monkeypatch.setattr(llm, "make_batch_client", lambda cfg: made.setdefault("client", object()))
    monkeypatch.setattr(analyze_mod, "analyze",
                        lambda con, cfg, **kw: {"classified": 0, "rechecked": 0,
                                               "pairs": 0, "halted_on_budget": False})
    rc = cli.main(["analyze", "--db", str(tmp_path / "m.sqlite"),
                   "--models-config", str(_models_yaml(tmp_path))])
    assert rc == 0 and "client" in made
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_cli_analyze.py::test_cli_analyze_uses_backend_factory -v`
Expected: FAIL (`_cmd_analyze` still constructs `AnthropicBatchClient()` directly).

- [ ] **Step 3: Implement**

In `cli.py` `_cmd_analyze`, replace the hardcoded client construction with the factory:

```python
        embedder=embedder, batch_client=llm.make_batch_client(cfg),
```

(Remove the now-unused direct `llm.AnthropicBatchClient()` construction.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_cli_analyze.py -v && make py-check`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_analyze.py
git commit -m "feat: select analyze backend via config factory"
```

---

### Task 5: Docs + smoke run

**Files:**
- Modify: `README.md`, `docs/superpowers/plans/2026-06-29-plan-2-smoke-notes.md`

**Interfaces:** none (docs + a manual run).

- [ ] **Step 1: Update docs**

In the README "Analysis pipeline (P2)" section, note that the default `backend: claude_cli` uses `claude -p` (Claude Code auth, no API key) and that setting `backend: anthropic_batch` + `ANTHROPIC_API_KEY` switches to the Batch API (#18). In the smoke-notes runbook, update the prerequisites: no API key needed under `claude_cli`; the `claude` CLI must be installed and logged in.

- [ ] **Step 2: Commit docs**

```bash
git add README.md docs/superpowers/plans/2026-06-29-plan-2-smoke-notes.md
git commit -m "docs: document claude_cli backend and update smoke prerequisites"
```

- [ ] **Step 3: Run the smoke test (controller-executed, real `claude -p`)**

This is the definition-of-done validation. It uses Claude Code auth (no API key) and real subscription spend (expected ~$1–2 for shinytest2). Run:

```bash
uv run triage-verse sync --repo rstudio/shinytest2
uv run triage-verse analyze --repo rstudio/shinytest2 --wait
```

Verify and record in the smoke-notes runbook:
- `sqlite3 .data/mirror.sqlite "SELECT stage, model, COUNT(*), ROUND(SUM(usd),4) FROM spend GROUP BY stage, model"` → non-zero USD, sourced from `total_cost_usd`.
- `classifications` and `dedup_verdicts` populated; a valid `.data/proposals/*/*.jsonl` with real `action` values.
- The breaker untripped (set `max_usd_per_day` generously first if needed).
- The per-call cost with `--system-prompt` replacement (confirm the overhead reduction vs the ~$0.019 baseline).

- [ ] **Step 4: Commit the recorded results**

```bash
git add docs/superpowers/plans/2026-06-29-plan-2-smoke-notes.md
git commit -m "docs: record claude_cli smoke-run results on shinytest2"
```

---

## Self-Review

**Spec coverage:** backend config field → Task 1; `cost_usd` + spend → Task 2; `ClaudeCliClient` (invocation, tools-disabled, `--system-prompt`, parse/validate/retry/skip, cost from `total_cost_usd`, model alias) → Task 3; factory + wiring → Task 4; docs + smoke run → Task 5. Injection safety (tools disabled + schema validation) is in Task 3 with a probe step. `jsonschema` dep in Task 3.

**Placeholder scan:** none — every step has concrete code/commands. The one verification step (Task 3 Step 5) is a security probe with a defined expected result and a fallback flag, not a logic placeholder.

**Type consistency:** `BatchResult.cost_usd` (Task 2) is read by `analyze` (Task 2) and set by `ClaudeCliClient` (Task 3). `record_spend(..., cost_usd=None)` signature consistent across Task 2 and its `analyze` call site. `make_batch_client(cfg)` (Task 3) consumed by `_cmd_analyze` (Task 4). `ModelsConfig.backend` (Task 1) read by `make_batch_client` (Task 3). Synthetic `_CliMessage.content[].text` is JSON that `extract_json`/`classify.parse`/`dedup.parse` read unchanged.
