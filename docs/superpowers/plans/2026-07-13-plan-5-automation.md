# Plan 5: Steady-state automation + escalation tiers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the steady-state loop (state bus + scheduled-but-dormant Action), Tier 1 "already fixed?" checks, Tier 2 draft-PR sessions, and graduated autonomy — one PR, four serial slices.

**Architecture:** New focused modules under `src/triage_verse/` (`state.py`, `steady_state.py`, `tier1.py`, `tier2.py`, `autonomy.py`), each a pure-logic core plus thin CLI wiring in `cli.py`. All GitHub/git/`claude` access goes through injectable `run_gh`/`run_git`/`runner` callables so every test runs offline. Two dormant GitHub workflows (`workflow_dispatch` only). The executor gains an `--auto` path that reuses the Plan 4 machinery via synthetic decisions.

**Tech Stack:** Python 3.14, stdlib only (argparse/json/sqlite3/pathlib/uuid/datetime/subprocess), `gh` + `git` + `claude` CLIs at runtime, pytest.

**Spec:** `docs/superpowers/specs/2026-07-13-plan-5-automation-design.md` — read it before starting any task.

## Global Constraints

- **Nothing in CI mutates GitHub issues.** The executor and `execute --auto` run locally only. CI produces proposals and (Tier 2) draft PRs.
- **Workflows ship dormant:** `workflow_dispatch` trigger only; any `schedule:`/cron line is present but commented out.
- **CI model auth = `CLAUDE_CODE_OAUTH_TOKEN`** repo secret, never the API key.
- **Every label this program introduces uses the `ai-triage:` prefix.** The Tier 2 marker is `ai-triage:fix-requested` and must NOT appear in `allowed_safe_output_labels` (pipeline may never propose it).
- **`triage-state` branch is the state bus** for `proposals/`, `decisions/`, `results/` JSONL and `cursors.json`; merge strategy is exact-line-set union. Mirror snapshots stay on Releases unchanged (dated restore points kept).
- **Autonomy v1: only `add-label` and `set-priority` are ever eligible for auto-apply.** `close`/`close-duplicate` stay human-gated at any precision. Promotion is an explicit commit to `config/autonomy.yaml`.
- Dry-run remains the default for anything that mutates; `--apply` opts in (matches Plan 4).
- Run `uv run pytest tests/ -q` for tests; `make check` before the final commit (ruff, pyright, pytest, `validate-yaml`, `compile-scripts`, node tests).
- Code style: `from __future__ import annotations`, module docstring first line, type hints on public functions (pyright runs in CI); stdlib `logging` in library code (CLI handlers may `print`).
- Directory env-var conventions already in use: `TRIAGE_VERSE_DB` (`.data/mirror.sqlite`), `TRIAGE_VERSE_PROPOSALS` (`.data/proposals`), `TRIAGE_VERSE_DECISIONS` (`.data/decisions`), `TRIAGE_VERSE_RESULTS` (`.data/results`).

---

## Existing interfaces the tasks build on (read-only reference)

- `jsonl_log.append_weekly(records: list[dict], base_dir, *, today: str | None = None) -> pathlib.Path` — appends to `<base>/<ISO year>/W<ISO week>.jsonl`.
- `review_queue.iter_jsonl_records(base_dir) -> list[dict]` — every `**/*.jsonl` record under a dir (skips malformed lines). Also `load_undecided(proposals_dir, decisions_dir, con, results_dir=None)`.
- `proposals.write(records, base_dir, *, today=None)`; proposal record shape: `{id, repo, issue, issue_updated_at, run_id, model, confidence, evidence, action, params, rationale}`.
- `decisions.record(proposal: dict, verdict: str, *, params=None) -> dict` → `{id, proposal_id, repo, issue, action, params, verdict, confidence, decided_at}`; `decisions.write(records, base_dir, *, today=None)`.
- `config.load_models_config(path) -> ModelsConfig` (frozen dataclass); `config.load_repos(path) -> list[Repo]` with `.full` = `"owner/name"`.
- `db.connect(path)`, `db.get_issue(con, repo, number)`, `db.get_comments(con, repo, number)`, `db.get_cursor(con, repo, kind)` (kind ∈ `issues|prs|comments`), `db.start_run(con, kind) -> run_id`, `db.insert_spend(...)`, `db.today_spend_usd(con) -> float`.
- `spend.record_spend(con, run_id, stage, model, pricing, usage, cost_usd=None) -> float`; `spend.breaker_tripped(con, cfg) -> bool`.
- `gh.run_gh(args: list[str], *, input=None, retries=5, sleep=time.sleep) -> str` — runs `["gh", *args]`, raises `gh.GhError`.
- `executor.execute(con, *, decisions_dir, proposals_dir, results_dir, run_gh, apply=False, repo=None, limit=None, labels_path=..., templates_dir=..., pace=time.sleep, log=print) -> {"batch_id", "counts"}`; `executor.select_executable(decisions, results)`; `executor._now() -> "%Y-%m-%dT%H:%M:%SZ"`.
- Mirror tables: `classifications(repo, number, close_candidate_json, confidence, run_id, ...)`, `prs(repo, number, merged, closing_issue_refs_json, ...)`, `issues(repo, number, state, updated_at, labels_json, ...)`.
- `claude -p` invocation pattern (from `llm.py:150`): `subprocess.run(["claude", "-p", prompt, *args], capture_output=True, text=True, timeout=...)`.
- Labels file `.github/triage/labels.yaml` sections: `classification`, `priority`, `workflow` (list of `{name,color,...}` or bare names — read the file), `reporting`, `allowed_safe_output_labels` (flat list of names).

---

### Task 1: Config — `tiers` and `autonomy` blocks in `ModelsConfig`

**Files:**
- Modify: `config/models.yaml`
- Modify: `src/triage_verse/config.py`
- Test: `tests/triage_verse/test_models_config.py` (append)

**Interfaces:**
- Produces: `ModelsConfig.tiers -> TiersConfig(tier1_max_per_day: int, tier2_max_per_week: int)` and `ModelsConfig.autonomy -> AutonomyConfig(min_decisions: int, min_precision: float, confidence_floor: float, audit_rate: float)`. Both optional in YAML with the spec defaults when absent.

- [ ] **Step 1: Write the failing test** (append to `tests/triage_verse/test_models_config.py`)

```python
def test_tiers_and_autonomy_defaults_and_overrides(tmp_path):
    from triage_verse import config

    base = (
        "backend: claude_cli\n"
        "embedding: {model: m, dim: 3, candidate_top_k: 1, cosine_threshold: 0.8}\n"
        "stages:\n"
        "  classify: {model: c, max_tokens: 1}\n"
        "  recheck: {model: r, max_tokens: 1}\n"
        "  dedup: {model: d, max_tokens: 1}\n"
        "batch: {max_requests_per_batch: 1, poll_interval_seconds: 1}\n"
        "spend: {batch_only: true, max_usd_per_day: 1, pricing: {}}\n"
    )
    p = tmp_path / "m.yaml"
    p.write_text(base, encoding="utf-8")
    cfg = config.load_models_config(p)
    assert cfg.tiers.tier1_max_per_day == 25
    assert cfg.tiers.tier2_max_per_week == 10
    assert cfg.autonomy.min_decisions == 200
    assert cfg.autonomy.min_precision == 0.98
    assert cfg.autonomy.confidence_floor == 0.9
    assert cfg.autonomy.audit_rate == 0.10

    p.write_text(
        base + "tiers: {tier1_max_per_day: 5, tier2_max_per_week: 2}\n"
        "autonomy: {min_decisions: 50, min_precision: 0.95,"
        " confidence_floor: 0.8, audit_rate: 0.25}\n",
        encoding="utf-8",
    )
    cfg = config.load_models_config(p)
    assert cfg.tiers.tier1_max_per_day == 5
    assert cfg.autonomy.min_decisions == 50
    assert cfg.autonomy.audit_rate == 0.25
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_models_config.py::test_tiers_and_autonomy_defaults_and_overrides -q`
Expected: FAIL — `AttributeError: 'ModelsConfig' object has no attribute 'tiers'`

- [ ] **Step 3: Implement in `src/triage_verse/config.py`**

Add two frozen dataclasses near `ModelsConfig` and two fields with defaults:

```python
@dataclass(frozen=True)
class TiersConfig:
    tier1_max_per_day: int = 25
    tier2_max_per_week: int = 10


@dataclass(frozen=True)
class AutonomyConfig:
    min_decisions: int = 200
    min_precision: float = 0.98
    confidence_floor: float = 0.9
    audit_rate: float = 0.10
```

Add fields to `ModelsConfig` (after `workers`):

```python
    tiers: TiersConfig = TiersConfig()
    autonomy: AutonomyConfig = AutonomyConfig()
```

In `load_models_config`, before the `return`, build them from optional keys and pass into the constructor:

```python
    t = data.get("tiers") or {}
    a = data.get("autonomy") or {}
```

Add to the `ModelsConfig(...)` call:

```python
        tiers=TiersConfig(**t),
        autonomy=AutonomyConfig(**a),
```

- [ ] **Step 4: Add the config block to `config/models.yaml`** (append at end)

```yaml
tiers:
  tier1_max_per_day: 25
  tier2_max_per_week: 10
autonomy:
  min_decisions: 200
  min_precision: 0.98
  confidence_floor: 0.9
  audit_rate: 0.10
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_models_config.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add config/models.yaml src/triage_verse/config.py tests/triage_verse/test_models_config.py
git commit -m "feat(config): tiers and autonomy config blocks"
```

---

### Task 2: State bus — union-merge core (`state.py`, pure functions)

**Files:**
- Create: `src/triage_verse/state.py`
- Test: `tests/triage_verse/test_state_merge.py`

**Interfaces:**
- Produces:
  - `state.union_merge_lines(existing: str, incoming: str) -> str` — returns existing content plus every incoming line not already present (exact-string match), preserving order; guarantees trailing newline per line; empty inputs handled.
  - `state.STATE_FILES = ("proposals", "decisions", "results")` (the JSONL subtrees) and `state.CURSORS_FILE = "cursors.json"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_state_merge.py
"""Union-merge property tests for the triage-state bus."""

from triage_verse import state


def test_union_merge_adds_only_new_lines():
    existing = '{"id": "a"}\n{"id": "b"}\n'
    incoming = '{"id": "b"}\n{"id": "c"}\n'
    merged = state.union_merge_lines(existing, incoming)
    assert merged == '{"id": "a"}\n{"id": "b"}\n{"id": "c"}\n'


def test_union_merge_is_idempotent():
    a = '{"id": "x"}\n{"id": "y"}\n'
    assert state.union_merge_lines(a, a) == a


def test_union_merge_handles_empty_sides():
    assert state.union_merge_lines("", '{"id": "a"}\n') == '{"id": "a"}\n'
    assert state.union_merge_lines('{"id": "a"}\n', "") == '{"id": "a"}\n'
    assert state.union_merge_lines("", "") == ""


def test_union_merge_tolerates_missing_final_newline():
    merged = state.union_merge_lines('{"id": "a"}', '{"id": "b"}')
    assert merged == '{"id": "a"}\n{"id": "b"}\n'


def test_union_merge_preserves_existing_order_and_dedups_incoming():
    existing = "l1\nl2\n"
    incoming = "l3\nl2\nl3\nl4\n"
    assert state.union_merge_lines(existing, incoming) == "l1\nl2\nl3\nl4\n"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_state_merge.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_verse.state'`

- [ ] **Step 3: Implement `src/triage_verse/state.py`**

```python
"""Sync append-only JSONL state + cursors with the triage-state branch."""

from __future__ import annotations

STATE_FILES = ("proposals", "decisions", "results")
CURSORS_FILE = "cursors.json"


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def union_merge_lines(existing: str, incoming: str) -> str:
    """Existing lines plus incoming lines not already present, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for ln in _lines(existing) + _lines(incoming):
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return "".join(ln + "\n" for ln in out)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_state_merge.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/state.py tests/triage_verse/test_state_merge.py
git commit -m "feat(state): union-merge core for triage-state bus"
```

---

### Task 3: State bus — git push/pull + cursors export + CLI

**Files:**
- Modify: `src/triage_verse/state.py`
- Modify: `src/triage_verse/cli.py`
- Test: `tests/triage_verse/test_state_sync.py`

**Interfaces:**
- Consumes: `state.union_merge_lines`, `state.STATE_FILES`, `state.CURSORS_FILE`; `db.get_cursor`; `review_queue.iter_jsonl_records` is NOT used here.
- Produces:
  - `state.export_cursors(con, repos: list[str], *, now: str) -> dict` — `{"exported_at": now, "repos": {repo: {"issues": .., "prs": .., "comments": ..}}}`.
  - `state.pull(*, data_dir, work_dir, run_git, branch="triage-state") -> dict` — union-merges branch files into `<data_dir>/{proposals,decisions,results}` and writes `cursors.json`; returns `{"files_updated": int}`.
  - `state.push(con, repos, *, data_dir, work_dir, run_git, branch="triage-state", now, log=print) -> dict` — pulls first, union-merges `<data_dir>` state into the branch worktree, refreshes `cursors.json` from `export_cursors`, commits + pushes only if changed; returns `{"pushed": bool, "records": int}`.
  - `run_git(args: list[str], *, cwd: str | None = None) -> str` shape (injectable; production wraps `subprocess.run(["git", *args], ...)`).

- [ ] **Step 1: Write the failing tests** (use a real local bare repo as the "remote" so git mechanics are exercised without network)

```python
# tests/triage_verse/test_state_sync.py
"""Push/pull round-trip for the triage-state bus against a local bare repo."""

import json
import pathlib
import subprocess

from triage_verse import db, state


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _run_git_factory(default_cwd):
    def run_git(args, *, cwd=None):
        return _git(args, cwd or default_cwd)
    return run_git


def _init_remote(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "triage-state", "."], remote)
    return remote


def _seed_data(data_dir, sub, year_week, records):
    d = data_dir / sub / "2026"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{year_week}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def test_push_then_pull_round_trips_records(tmp_path):
    remote = _init_remote(tmp_path)
    con = db.connect(":memory:")
    con.execute("INSERT INTO repos (repo, issues_cursor) VALUES ('o/r', 'CUR1')")

    data_a = tmp_path / "a"
    _seed_data(data_a, "proposals", "W01", [{"id": "p1"}])
    work_a = tmp_path / "work_a"
    run_git_a = _run_git_factory(str(work_a))
    # clone points at the bare remote
    _git(["clone", str(remote), str(work_a)], tmp_path)
    res = state.push(
        con, ["o/r"], data_dir=data_a, work_dir=work_a, run_git=run_git_a,
        now="2026-07-13T00:00:00Z",
    )
    assert res["pushed"] is True

    # second machine pulls
    data_b = tmp_path / "b"
    data_b.mkdir()
    work_b = tmp_path / "work_b"
    _git(["clone", str(remote), str(work_b)], tmp_path)
    run_git_b = _run_git_factory(str(work_b))
    state.pull(data_dir=data_b, work_dir=work_b, run_git=run_git_b)
    got = (data_b / "proposals" / "2026" / "W01.jsonl").read_text(encoding="utf-8")
    assert json.loads(got.strip())["id"] == "p1"
    cursors = json.loads((data_b / "cursors.json").read_text(encoding="utf-8"))
    assert cursors["repos"]["o/r"]["issues"] == "CUR1"


def test_push_with_no_changes_makes_no_commit(tmp_path):
    remote = _init_remote(tmp_path)
    con = db.connect(":memory:")
    data = tmp_path / "d"
    _seed_data(data, "decisions", "W01", [{"id": "d1"}])
    work = tmp_path / "work"
    _git(["clone", str(remote), str(work)], tmp_path)
    rg = _run_git_factory(str(work))
    state.push(con, [], data_dir=data, work_dir=work, run_git=rg, now="2026-07-13T00:00:00Z")
    before = _git(["rev-list", "--count", "HEAD"], work).strip()
    res = state.push(con, [], data_dir=data, work_dir=work, run_git=rg, now="2026-07-13T00:00:00Z")
    after = _git(["rev-list", "--count", "HEAD"], work).strip()
    assert res["pushed"] is False
    assert before == after


def test_export_cursors_shape():
    con = db.connect(":memory:")
    con.execute(
        "INSERT INTO repos (repo, issues_cursor, prs_cursor, comments_cursor)"
        " VALUES ('o/r', 'I', 'P', 'C')"
    )
    out = state.export_cursors(con, ["o/r"], now="2026-07-13T00:00:00Z")
    assert out == {
        "exported_at": "2026-07-13T00:00:00Z",
        "repos": {"o/r": {"issues": "I", "prs": "P", "comments": "C"}},
    }
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_state_sync.py -q`
Expected: FAIL — `AttributeError: module 'triage_verse.state' has no attribute 'export_cursors'`

- [ ] **Step 3: Implement in `src/triage_verse/state.py`** (append; add imports)

```python
import json
import pathlib
from typing import Any, Callable

from . import db as db_mod

RunGit = Callable[..., str]


def export_cursors(con, repos: list[str], *, now: str) -> dict:
    out: dict[str, dict] = {}
    for repo in repos:
        out[repo] = {
            "issues": db_mod.get_cursor(con, repo, "issues"),
            "prs": db_mod.get_cursor(con, repo, "prs"),
            "comments": db_mod.get_cursor(con, repo, "comments"),
        }
    return {"exported_at": now, "repos": out}


def _jsonl_paths(base: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for sub in STATE_FILES for p in (base / sub).glob("**/*.jsonl"))


def _rel(base: pathlib.Path, path: pathlib.Path) -> str:
    return str(path.relative_to(base))


def pull(*, data_dir, work_dir, run_git: RunGit, branch: str = "triage-state") -> dict:
    data_dir = pathlib.Path(data_dir)
    work_dir = pathlib.Path(work_dir)
    run_git(["fetch", "origin", branch], cwd=str(work_dir))
    run_git(["checkout", branch], cwd=str(work_dir))
    updated = 0
    for src in _jsonl_paths(work_dir):
        rel = _rel(work_dir, src)
        dst = data_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        existing = dst.read_text(encoding="utf-8") if dst.exists() else ""
        merged = union_merge_lines(existing, src.read_text(encoding="utf-8"))
        if merged != existing:
            dst.write_text(merged, encoding="utf-8")
            updated += 1
    cur = work_dir / CURSORS_FILE
    if cur.exists():
        (data_dir / CURSORS_FILE).write_text(cur.read_text(encoding="utf-8"), encoding="utf-8")
    return {"files_updated": updated}


def push(
    con, repos, *, data_dir, work_dir, run_git: RunGit,
    branch: str = "triage-state", now: str, log: Callable[[str], None] = print,
) -> dict:
    data_dir = pathlib.Path(data_dir)
    work_dir = pathlib.Path(work_dir)
    # Pull first so we never clobber the remote.
    run_git(["fetch", "origin", branch], cwd=str(work_dir))
    run_git(["checkout", branch], cwd=str(work_dir))
    records = 0
    for src in _jsonl_paths(data_dir):
        rel = _rel(data_dir, src)
        dst = work_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        existing = dst.read_text(encoding="utf-8") if dst.exists() else ""
        merged = union_merge_lines(existing, src.read_text(encoding="utf-8"))
        if merged != existing:
            dst.write_text(merged, encoding="utf-8")
        records += len(_lines(src.read_text(encoding="utf-8")))
    (work_dir / CURSORS_FILE).write_text(
        json.dumps(export_cursors(con, list(repos), now=now), indent=2) + "\n",
        encoding="utf-8",
    )
    run_git(["add", "-A"], cwd=str(work_dir))
    status = run_git(["status", "--porcelain"], cwd=str(work_dir))
    if not status.strip():
        return {"pushed": False, "records": records}
    run_git(["commit", "-m", f"state: sync {records} records"], cwd=str(work_dir))
    run_git(["push", "origin", branch], cwd=str(work_dir))
    return {"pushed": True, "records": records}
```

Note: production `run_git` (define in cli.py) is `subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout`. The bare-remote fixture exercises fetch/checkout/commit/push for real.

- [ ] **Step 4: Wire CLI `state pull` / `state push` in `src/triage_verse/cli.py`**

Add a `run_git` helper and handlers. The work dir is a persistent clone at `.data/triage-state` (created via `gh repo clone` on first use); env `TRIAGE_VERSE_STATE_WORKDIR` overrides. Handler:

```python
def _run_git(args, *, cwd=None):
    import subprocess
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", check=True,
    ).stdout


def _ensure_state_clone(work_dir: str, branch: str) -> None:
    import pathlib
    if pathlib.Path(work_dir, ".git").exists():
        return
    origin = gh.run_gh(["repo", "view", "--json", "url", "-q", ".url"]).strip()
    _run_git(["clone", "--branch", branch, "--single-branch", origin, work_dir])


def _cmd_state_pull(args):
    from . import state
    work = os.environ.get("TRIAGE_VERSE_STATE_WORKDIR", ".data/triage-state")
    _ensure_state_clone(work, args.branch)
    res = state.pull(data_dir=args.data_dir, work_dir=work, run_git=_run_git, branch=args.branch)
    print(f"pulled: {res['files_updated']} files updated")
    return 0


def _cmd_state_push(args):
    from . import state
    con = _open_db(args.db)
    repos = [r.full for r in config.load_repos(args.config)]
    work = os.environ.get("TRIAGE_VERSE_STATE_WORKDIR", ".data/triage-state")
    _ensure_state_clone(work, args.branch)
    res = state.push(
        con, repos, data_dir=args.data_dir, work_dir=work, run_git=_run_git,
        branch=args.branch, now=_state_now(),
    )
    print(f"push: {'committed' if res['pushed'] else 'no changes'} ({res['records']} records)")
    return 0
```

Add `_state_now()` helper (`from datetime import datetime, timezone; return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`). In `build_parser`, add a `state` subparser with `pull`/`push` sub-subcommands; each takes `--branch` (default `triage-state`), `--data-dir` (default `.data`), and `push` also `--db`/`--config` (defaults `DEFAULT_DB`/`DEFAULT_CONFIG`). A CLI-level test is not required here (git mechanics covered in state tests); just ensure `build_parser().parse_args(["state","push"])` resolves — add that assertion to `test_state_sync.py`:

```python
def test_state_cli_parses():
    from triage_verse import cli
    args = cli.build_parser().parse_args(["state", "push"])
    assert args.func is not None
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_state_sync.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/state.py src/triage_verse/cli.py tests/triage_verse/test_state_sync.py
git commit -m "feat(state): triage-state push/pull with cursors export"
```

---

### Task 4: `steady_state.py` orchestration + CLI

**Files:**
- Create: `src/triage_verse/steady_state.py`
- Modify: `src/triage_verse/cli.py`
- Test: `tests/triage_verse/test_steady_state.py`

**Interfaces:**
- Consumes: nothing new; takes injected stage callables.
- Produces: `steady_state.run(stages: list[tuple[str, Callable[[], None]]], *, log=print) -> dict` — runs each `(name, fn)` in order; returns `{"completed": [names], "failed": name | None}`; a stage raising stops the loop, records `failed`, and re-raises after logging is NOT done — instead returns the dict with `failed` set and the exception message in `{"error": str}`. Completed stages are not rolled back.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_steady_state.py
"""Stage orchestration for the steady-state loop."""

from triage_verse import steady_state


def test_runs_all_stages_in_order():
    calls = []
    stages = [("a", lambda: calls.append("a")), ("b", lambda: calls.append("b"))]
    res = steady_state.run(stages, log=lambda *a: None)
    assert calls == ["a", "b"]
    assert res["completed"] == ["a", "b"]
    assert res["failed"] is None


def test_stops_at_failing_stage_but_keeps_completed():
    calls = []

    def boom():
        raise RuntimeError("kaboom")

    stages = [
        ("a", lambda: calls.append("a")),
        ("b", boom),
        ("c", lambda: calls.append("c")),
    ]
    res = steady_state.run(stages, log=lambda *a: None)
    assert calls == ["a"]
    assert res["completed"] == ["a"]
    assert res["failed"] == "b"
    assert "kaboom" in res["error"]
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_steady_state.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/triage_verse/steady_state.py`**

```python
"""Ordered stage runner for the steady-state loop (sync/analyze/tier1/state/snapshot)."""

from __future__ import annotations

from typing import Callable


def run(stages: list[tuple[str, Callable[[], None]]], *, log: Callable[[str], None] = print) -> dict:
    completed: list[str] = []
    for name, fn in stages:
        log(f"stage: {name} - starting")
        try:
            fn()
        except Exception as exc:  # a failed stage stops the loop; no rollback
            log(f"stage: {name} - FAILED: {exc}")
            return {"completed": completed, "failed": name, "error": str(exc)}
        completed.append(name)
        log(f"stage: {name} - done")
    return {"completed": completed, "failed": None}
```

- [ ] **Step 4: Wire CLI `steady-state` in `cli.py`**

Handler assembles the stage list from existing commands and calls `steady_state.run`. Reuse existing handler bodies by calling the underlying module functions (not argparse). Skeleton:

```python
def _cmd_steady_state(args):
    from . import state, steady_state, tier1  # tier1 wired in Task 7
    con = _open_db(args.db)
    repos = [r.full for r in config.load_repos(args.config)]
    work = os.environ.get("TRIAGE_VERSE_STATE_WORKDIR", ".data/triage-state")

    def _pull():
        _ensure_state_clone(work, args.branch)
        state.pull(data_dir=args.data_dir, work_dir=work, run_git=_run_git, branch=args.branch)

    def _sync():
        sync_mod.sync_all(con, repos, full=False, log=print)

    def _analyze():
        _run_analyze(args)  # extract existing _cmd_analyze body into _run_analyze(args, con)

    def _tier1():
        if not args.no_tier1:
            tier1.run(con, repos, cfg=config.load_models_config(args.models_config),
                      proposals_dir=args.proposals_dir, run_gh=gh.run_gh, log=print)

    def _push():
        state.push(con, repos, data_dir=args.data_dir, work_dir=work, run_git=_run_git,
                   branch=args.branch, now=_state_now())

    def _snapshot():
        snapshot_mod.publish(args.db, dated=False)

    stages = [("state-pull", _pull), ("sync", _sync), ("embed-analyze", _analyze),
              ("tier1", _tier1), ("state-push", _push), ("snapshot", _snapshot)]
    if args.dry_run:
        for name, _ in stages:
            print(f"would run: {name}")
        return 0
    res = steady_state.run(stages)
    return 1 if res["failed"] else 0
```

Add the `steady-state` subparser: `--db --config --models-config --proposals-dir --data-dir --branch --no-tier1 --dry-run` with the same defaults as the sibling commands. Because `_tier1` references `tier1.run` (Task 7), guard the import inside the closure so this task's tests don't need tier1 — but the closure isn't invoked in the dry-run parse test. Add to the test file:

```python
def test_steady_state_cli_dry_run(monkeypatch, capsys, tmp_path):
    from triage_verse import cli
    monkeypatch.setenv("TRIAGE_VERSE_DB", str(tmp_path / "m.sqlite"))
    rc = cli.main(["steady-state", "--dry-run", "--config", "config/repos.yaml"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would run: sync" in out and "would run: tier1" in out
```

(Refactor `_cmd_analyze`'s body into a reusable `_run_analyze(args)` that both `_cmd_analyze` and `_cmd_steady_state` call, to avoid duplication.)

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_steady_state.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/steady_state.py src/triage_verse/cli.py tests/triage_verse/test_steady_state.py
git commit -m "feat(steady-state): ordered loop runner + CLI"
```

---

### Task 5: `steady-state.yml` workflow (dormant)

**Files:**
- Create: `.github/workflows/steady-state.yml`
- Test: `tests/triage_verse/test_workflows.py`

**Interfaces:** none (YAML + a dormancy regression test).

- [ ] **Step 1: Write the failing test**

```python
# tests/triage_verse/test_workflows.py
"""Dormancy + shape guards for the Plan 5 workflows."""

import pathlib

import yaml

WF = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load(name):
    text = (WF / name).read_text(encoding="utf-8")
    # PyYAML parses `on:` as boolean True key; keep both the parsed doc and raw text.
    return yaml.safe_load(text), text


def test_steady_state_is_dormant_dispatch_only():
    doc, text = _load("steady-state.yml")
    triggers = doc.get(True, doc.get("on"))
    assert "workflow_dispatch" in triggers
    # No active schedule: any cron line must be commented out.
    assert "schedule:" not in triggers if isinstance(triggers, dict) else True
    active_cron = [
        ln for ln in text.splitlines()
        if "cron:" in ln and not ln.strip().startswith("#")
    ]
    assert active_cron == []


def test_steady_state_has_no_issue_write_permission():
    doc, _ = _load("steady-state.yml")
    perms = doc.get("permissions", {})
    assert perms.get("issues", "none") != "write"
    assert perms.get("pull-requests", "none") != "write"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_workflows.py -q`
Expected: FAIL — `FileNotFoundError`.

- [ ] **Step 3: Write `.github/workflows/steady-state.yml`**

```yaml
name: Steady-state triage

# DORMANT: manual dispatch only. To activate the 12h cadence, uncomment the
# schedule block below.
on:
  workflow_dispatch:
  # schedule:
  #   - cron: "0 */12 * * *"

permissions:
  contents: write  # triage-state branch + mirror releases; NO issue/PR writes

jobs:
  steady-state:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
      - name: Install deps
        run: uv sync
      - name: Install Claude CLI
        run: npm install -g @anthropic-ai/claude-code
      - name: Bootstrap mirror
        run: uv run triage-verse snapshot bootstrap
        env:
          GH_TOKEN: ${{ github.token }}
      - name: Run steady-state loop
        run: uv run triage-verse steady-state
        env:
          GH_TOKEN: ${{ github.token }}
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      - name: Summary
        run: echo "steady-state run complete" >> "$GITHUB_STEP_SUMMARY"
```

- [ ] **Step 4: Run to verify pass + yaml validation**

Run: `uv run pytest tests/triage_verse/test_workflows.py -q && make validate-yaml`
Expected: tests pass; validate-yaml succeeds.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/steady-state.yml tests/triage_verse/test_workflows.py
git commit -m "feat(ci): dormant steady-state workflow (dispatch-only)"
```

---

### Task 6: Tier 1 — candidate selection

**Files:**
- Create: `src/triage_verse/tier1.py`
- Test: `tests/triage_verse/test_tier1_candidates.py`

**Interfaces:**
- Consumes: mirror tables `classifications`, `prs`, `issues`; `review_queue.iter_jsonl_records`.
- Produces: `tier1.select_candidates(con, repos, *, proposals_dir, limit) -> list[dict]` — each `{"repo", "issue"}`, oldest-first, capped at `limit`; excludes issues that already have a tier1 proposal-log entry (`origin == "tier1"`) for that issue.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_tier1_candidates.py
"""Tier 1 candidate selection from the mirror."""

from triage_verse import db, jsonl_log, tier1


def _open_issue(con, repo, number, updated="2026-01-01T00:00:00Z"):
    con.execute(
        "INSERT INTO issues (repo, number, title, state, updated_at, created_at,"
        " labels_json) VALUES (?,?,?,?,?,?,?)",
        (repo, number, "t", "OPEN", updated, updated, "[]"),
    )


def test_selects_fixed_close_candidates_and_merged_pr_refs(tmp_path):
    con = db.connect(":memory:")
    _open_issue(con, "o/r", 1)
    _open_issue(con, "o/r", 2)
    _open_issue(con, "o/r", 3)
    con.execute(
        "INSERT INTO classifications (repo, number, clf_hash, type, priority,"
        " assessment, close_candidate_json, confidence, model, run_id, at)"
        " VALUES ('o/r',1,'h','bug','Low','a',?,0.8,'m','run','2026-01-01T00:00:00Z')",
        ('{"reason": "fixed", "rationale": "r", "confidence": 0.8}',),
    )
    # issue 2 referenced by a merged PR
    con.execute(
        "INSERT INTO prs (repo, number, title, state, merged, created_at,"
        " updated_at, closing_issue_refs_json, labels_json)"
        " VALUES ('o/r',99,'p','MERGED',1,'x','x','[2]','[]')"
    )
    cands = tier1.select_candidates(con, ["o/r"], proposals_dir=tmp_path, limit=25)
    nums = {c["issue"] for c in cands}
    assert nums == {1, 2}  # 3 has no signal


def test_excludes_issues_with_existing_tier1_proposal(tmp_path):
    con = db.connect(":memory:")
    _open_issue(con, "o/r", 1)
    con.execute(
        "INSERT INTO classifications (repo, number, clf_hash, type, priority,"
        " assessment, close_candidate_json, confidence, model, run_id, at)"
        " VALUES ('o/r',1,'h','bug','Low','a',?,0.8,'m','run','2026-01-01T00:00:00Z')",
        ('{"reason": "fixed", "rationale": "r", "confidence": 0.8}',),
    )
    jsonl_log.append_weekly(
        [{"id": "x", "repo": "o/r", "issue": 1, "origin": "tier1", "action": "no-op"}],
        tmp_path,
    )
    cands = tier1.select_candidates(con, ["o/r"], proposals_dir=tmp_path, limit=25)
    assert cands == []


def test_limit_caps_and_orders_oldest_first(tmp_path):
    con = db.connect(":memory:")
    _open_issue(con, "o/r", 1, "2026-03-01T00:00:00Z")
    _open_issue(con, "o/r", 2, "2026-01-01T00:00:00Z")
    for n in (1, 2):
        con.execute(
            "INSERT INTO classifications (repo, number, clf_hash, type, priority,"
            " assessment, close_candidate_json, confidence, model, run_id, at)"
            " VALUES ('o/r',?,'h','bug','Low','a',?,0.8,'m','run','2026-01-01T00:00:00Z')",
            (n, '{"reason": "fixed", "rationale": "r", "confidence": 0.8}'),
        )
    cands = tier1.select_candidates(con, ["o/r"], proposals_dir=tmp_path, limit=1)
    assert cands == [{"repo": "o/r", "issue": 2}]  # oldest updated_at first
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_tier1_candidates.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/triage_verse/tier1.py`** (selection only for now)

```python
"""Tier 1 "already fixed?" checks: candidate selection + read-only session."""

from __future__ import annotations

import json

from . import review_queue


def select_candidates(con, repos, *, proposals_dir, limit: int) -> list[dict]:
    seen_tier1 = {
        (r.get("repo"), r.get("issue"))
        for r in review_queue.iter_jsonl_records(proposals_dir)
        if r.get("origin") == "tier1"
    }
    placeholders = ",".join("?" for _ in repos)
    rows = con.execute(
        f"""
        SELECT i.repo AS repo, i.number AS number, i.updated_at AS updated_at
        FROM issues i
        WHERE i.state = 'OPEN' AND i.repo IN ({placeholders})
          AND (
            EXISTS (
              SELECT 1 FROM classifications c
              WHERE c.repo = i.repo AND c.number = i.number
                AND c.close_candidate_json IS NOT NULL
                AND json_extract(c.close_candidate_json, '$.reason') = 'fixed'
            )
            OR EXISTS (
              SELECT 1 FROM prs p
              WHERE p.repo = i.repo AND p.merged = 1
                AND EXISTS (
                  SELECT 1 FROM json_each(p.closing_issue_refs_json) je
                  WHERE je.value = i.number
                )
            )
          )
        ORDER BY i.updated_at ASC, i.number ASC
        """,
        list(repos),
    ).fetchall()
    out = []
    for row in rows:
        key = (row["repo"], row["number"])
        if key in seen_tier1:
            continue
        out.append({"repo": row["repo"], "issue": row["number"]})
        if len(out) >= limit:
            break
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_tier1_candidates.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/tier1.py tests/triage_verse/test_tier1_candidates.py
git commit -m "feat(tier1): candidate selection from mirror"
```

---

### Task 7: Tier 1 — session runner, proposal emission, cap + breaker, CLI

**Files:**
- Modify: `src/triage_verse/tier1.py`
- Modify: `src/triage_verse/cli.py`
- Test: `tests/triage_verse/test_tier1_run.py`

**Interfaces:**
- Consumes: `tier1.select_candidates`; `db.get_issue`, `db.get_comments`, `db.start_run`, `db.today_spend_usd`; `spend.breaker_tripped`; `proposals.write`; `config.ModelsConfig`.
- Produces:
  - `tier1.build_prompt(issue: dict, comments: list[dict]) -> str`.
  - `tier1.parse_session(text: str) -> dict` — extracts the JSON object; raises `ValueError` if none. Validates `verdict ∈ {fixed, not-fixed, unclear}`.
  - `tier1.run(con, repos, *, cfg, proposals_dir, run_gh, runner=<default claude -p>, checkout=<default>, today=None, log=print) -> dict` — returns `{"sessions": int, "proposals": int, "halted_on_budget": bool}`. For each candidate (capped by `cfg.tiers.tier1_max_per_day` minus today's tier1 proposals): stop if `spend.breaker_tripped`; run session; `fixed` → `close` proposal with `origin="tier1"`; else a `no-op` tier1 log record. `runner(repo_dir, prompt) -> (text, cost_usd)`; `checkout(repo, cache_dir) -> repo_dir`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_tier1_run.py
"""Tier 1 session run: parsing, proposal emission, caps."""

import pytest

from triage_verse import config, db, review_queue, tier1


def _seed_fixed(con, repo, number):
    con.execute(
        "INSERT INTO issues (repo, number, title, body, state, updated_at,"
        " created_at, labels_json) VALUES (?,?,?,?,?,?,?,?)",
        (repo, number, "Crash on load", "steps", "OPEN",
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "[]"),
    )
    con.execute(
        "INSERT INTO classifications (repo, number, clf_hash, type, priority,"
        " assessment, close_candidate_json, confidence, model, run_id, at)"
        " VALUES (?,?,'h','bug','Low','a',?,0.8,'m','run','2026-01-01T00:00:00Z')",
        (repo, number, '{"reason": "fixed", "rationale": "r", "confidence": 0.8}'),
    )


def _cfg(tmp_path):
    return config.load_models_config("config/models.yaml")


def test_parse_session_valid_and_invalid():
    ok = tier1.parse_session('{"verdict": "fixed", "fixed_in": "v1.2",'
                             ' "evidence": ["x"], "summary": "s", "confidence": 0.9}')
    assert ok["verdict"] == "fixed"
    with pytest.raises(ValueError):
        tier1.parse_session("no json here")
    with pytest.raises(ValueError):
        tier1.parse_session('{"verdict": "banana"}')


def test_run_emits_close_proposal_on_fixed(tmp_path):
    con = db.connect(":memory:")
    _seed_fixed(con, "o/r", 1)

    def runner(repo_dir, prompt):
        return ('{"verdict": "fixed", "fixed_in": "v1.2", "evidence":'
                ' ["https://github.com/o/r/commit/abc"], "summary": "fixed in v1.2",'
                ' "confidence": 0.92}'), 0.4

    def checkout(repo, cache_dir):
        return "/fake/repo"

    res = tier1.run(con, ["o/r"], cfg=_cfg(tmp_path), proposals_dir=tmp_path,
                    run_gh=lambda *a, **k: "", runner=runner, checkout=checkout,
                    log=lambda *a: None)
    assert res["sessions"] == 1 and res["proposals"] == 1
    recs = review_queue.iter_jsonl_records(tmp_path)
    close = [r for r in recs if r["action"] == "close"]
    assert close[0]["origin"] == "tier1"
    assert close[0]["params"]["reason"] == "fixed"
    assert close[0]["repo"] == "o/r" and close[0]["issue"] == 1


def test_run_records_noop_on_not_fixed(tmp_path):
    con = db.connect(":memory:")
    _seed_fixed(con, "o/r", 1)

    def runner(repo_dir, prompt):
        return '{"verdict": "not-fixed", "fixed_in": null, "evidence": [],' \
               ' "summary": "still broken", "confidence": 0.7}', 0.3

    res = tier1.run(con, ["o/r"], cfg=_cfg(tmp_path), proposals_dir=tmp_path,
                    run_gh=lambda *a, **k: "", runner=runner,
                    checkout=lambda r, c: "/fake", log=lambda *a: None)
    assert res["proposals"] == 0
    recs = review_queue.iter_jsonl_records(tmp_path)
    assert recs[0]["action"] == "no-op" and recs[0]["origin"] == "tier1"


def test_run_stops_when_breaker_tripped(tmp_path, monkeypatch):
    con = db.connect(":memory:")
    _seed_fixed(con, "o/r", 1)
    monkeypatch.setattr(tier1.spend, "breaker_tripped", lambda con, cfg: True)
    res = tier1.run(con, ["o/r"], cfg=_cfg(tmp_path), proposals_dir=tmp_path,
                    run_gh=lambda *a, **k: "", runner=lambda d, p: ("{}", 0.0),
                    checkout=lambda r, c: "/fake", log=lambda *a: None)
    assert res["sessions"] == 0 and res["halted_on_budget"] is True
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_tier1_run.py -q`
Expected: FAIL — `AttributeError: module 'triage_verse.tier1' has no attribute 'parse_session'`.

- [ ] **Step 3: Implement in `src/triage_verse/tier1.py`** (append; add imports)

```python
import subprocess
import uuid
from datetime import datetime, timezone

from . import db as db_mod
from . import prompts, proposals, spend

_VERDICTS = {"fixed", "not-fixed", "unclear"}
_CLI_TIMEOUT = 600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_prompt(issue: dict, comments: list[dict]) -> str:
    thread = "\n\n".join(f"@{c['author']}: {c['body']}" for c in comments)
    return (
        "You are checking whether a reported issue has ALREADY been fixed in this "
        "repository. Search NEWS/NEWS.md/changelog, `git log`, and merged history. "
        "Do not modify any files.\n\n"
        + prompts.delimit("ISSUE_TITLE", issue.get("title"))
        + "\n"
        + prompts.delimit("ISSUE_BODY", issue.get("body"))
        + "\n"
        + prompts.delimit("COMMENTS", thread)
        + "\n\nRespond with ONLY a JSON object: "
        '{"verdict": "fixed|not-fixed|unclear", "fixed_in": string|null, '
        '"evidence": [urls or commit shas], "summary": string, "confidence": number}.'
    )


def parse_session(text: str) -> dict:
    t = text.strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        raise ValueError("no JSON object in tier1 output")
    import json
    data = json.loads(t[i : j + 1])
    if data.get("verdict") not in _VERDICTS:
        raise ValueError(f"invalid tier1 verdict: {data.get('verdict')!r}")
    return data


def _default_checkout(repo: str, cache_dir: str) -> str:
    import pathlib
    dest = pathlib.Path(cache_dir) / repo.replace("/", "__")
    if (dest / ".git").exists():
        subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1"], check=False)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["gh", "repo", "clone", repo, str(dest), "--", "--depth", "1"], check=True
        )
    return str(dest)


def _default_runner(repo_dir: str, prompt: str) -> tuple[str, float]:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--add-dir", repo_dir,
         "--allowedTools", "Read,Grep,Glob,Bash(git log:*),Bash(git show:*)"],
        capture_output=True, text=True, cwd=repo_dir, timeout=_CLI_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout, 0.0  # cost parsed from stream-json in a later pass; 0 for now


def _today_tier1_count(records, today: str) -> int:
    return sum(
        1 for r in records
        if r.get("origin") == "tier1" and (r.get("created_at", "")[:10] == today)
    )


def run(con, repos, *, cfg, proposals_dir, run_gh, runner=_default_runner,
        checkout=_default_checkout, cache_dir=".data/checkouts", today=None,
        log=print) -> dict:
    from . import review_queue
    today = today or _now()[:10]
    cap = cfg.tiers.tier1_max_per_day
    existing = review_queue.iter_jsonl_records(proposals_dir)
    remaining = max(0, cap - _today_tier1_count(existing, today))
    cands = select_candidates(con, repos, proposals_dir=proposals_dir, limit=remaining)
    run_id = db_mod.start_run(con, "tier1")
    sessions = proposals_made = 0
    halted = False
    for c in cands:
        if spend.breaker_tripped(con, cfg):
            halted = True
            break
        issue = db_mod.get_issue(con, c["repo"], c["issue"])
        comments = [dict(r) for r in db_mod.get_comments(con, c["repo"], c["issue"])]
        repo_dir = checkout(c["repo"], cache_dir)
        text, cost = runner(repo_dir, build_prompt(dict(issue), comments))
        sessions += 1
        if cost:
            db_mod.insert_spend(con, run_id, "tier1", cfg.classify.model, 0, 0, 0, cost)
        try:
            verdict = parse_session(text)
        except ValueError as exc:
            log(f"tier1 {c['repo']}#{c['issue']}: unparseable ({exc})")
            _emit_noop(c, "unclear", proposals_dir)
            continue
        if verdict["verdict"] == "fixed":
            _emit_close(c, issue, verdict, proposals_dir, run_id)
            proposals_made += 1
        else:
            _emit_noop(c, verdict["verdict"], proposals_dir)
    return {"sessions": sessions, "proposals": proposals_made, "halted_on_budget": halted}


def _emit_close(c, issue, verdict, proposals_dir, run_id) -> None:
    evidence = [f"https://github.com/{c['repo']}/issues/{c['issue']}", *verdict.get("evidence", [])]
    rec = {
        "id": uuid.uuid4().hex,
        "repo": c["repo"], "issue": c["issue"],
        "issue_updated_at": issue["updated_at"],
        "run_id": run_id, "model": "tier1", "origin": "tier1",
        "confidence": verdict.get("confidence", 0.0),
        "evidence": evidence,
        "action": "close", "params": {"reason": "fixed"},
        "rationale": verdict.get("summary", ""),
        "created_at": _now(),
    }
    proposals.write([rec], proposals_dir)


def _emit_noop(c, verdict, proposals_dir) -> None:
    rec = {
        "id": uuid.uuid4().hex,
        "repo": c["repo"], "issue": c["issue"],
        "origin": "tier1", "action": "no-op", "verdict": verdict,
        "created_at": _now(),
    }
    proposals.write([rec], proposals_dir)
```

Note: `review_queue.load_undecided` already filters to `SUPPORTED_ACTIONS` (`add-label`/`set-priority`/`close`/`close-duplicate`), so `no-op` records are naturally ignored by the queue. Verify `db.get_comments` rows have `author`/`body` columns; if named differently, adapt `build_prompt`'s access (read `db.py` schema).

- [ ] **Step 4: Wire CLI `tier1`**

```python
def _cmd_tier1(args):
    con = _open_db(args.db)
    repos = [args.repo] if args.repo else [r.full for r in config.load_repos(args.config)]
    cfg = config.load_models_config(args.models_config)
    from . import tier1
    res = tier1.run(con, repos, cfg=cfg, proposals_dir=args.proposals_dir, run_gh=gh.run_gh)
    print(f"tier1: {res['sessions']} sessions, {res['proposals']} close proposals"
          f"{' (halted on budget)' if res['halted_on_budget'] else ''}")
    return 0
```

Add subparser `tier1`: `--db --config --models-config --repo --proposals-dir` (defaults as siblings). No `--limit` needed (cap comes from config); optional `--limit` may override — omit for YAGNI.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_tier1_run.py tests/triage_verse/test_tier1_candidates.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/tier1.py src/triage_verse/cli.py tests/triage_verse/test_tier1_run.py
git commit -m "feat(tier1): read-only session, close proposals, daily cap + breaker"
```

---

### Task 8: Tier 2 marker label + `tier2` CLI

**Files:**
- Modify: `.github/triage/labels.yaml`
- Create: `src/triage_verse/tier2.py`
- Modify: `src/triage_verse/cli.py`
- Test: `tests/triage_verse/test_tier2.py`

**Interfaces:**
- Produces: `tier2.request_fix(repo: str, number: int, *, run_gh, label="ai-triage:fix-requested") -> None` — adds the label via `gh issue edit`. `tier2.LABEL = "ai-triage:fix-requested"`.

- [ ] **Step 1: Add the label to `.github/triage/labels.yaml`**

In the `workflow:` section add an entry (match the section's existing item shape — read the file first; if items carry `color`/`meaning`, include them):

```yaml
  - name: ai-triage:fix-requested
    color: 5319E7
    meaning: A maintainer asked an AI agent to attempt a draft-PR fix (Tier 2).
```

Do NOT add it to `allowed_safe_output_labels`.

- [ ] **Step 2: Write the failing tests**

```python
# tests/triage_verse/test_tier2.py
"""Tier 2 label request + allowlist guard."""

import pathlib

import yaml

from triage_verse import tier2

LABELS = pathlib.Path(__file__).resolve().parents[2] / ".github" / "triage" / "labels.yaml"


def test_request_fix_adds_label_via_gh():
    calls = []
    tier2.request_fix("o/r", 7, run_gh=lambda args, **k: calls.append(args) or "")
    assert calls == [["issue", "edit", "7", "--repo", "o/r",
                      "--add-label", "ai-triage:fix-requested"]]


def test_marker_label_not_in_allowed_safe_output():
    doc = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    assert "ai-triage:fix-requested" not in doc.get("allowed_safe_output_labels", [])


def test_marker_label_is_defined_in_workflow_section():
    doc = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    names = {e["name"] if isinstance(e, dict) else e for e in doc.get("workflow", [])}
    assert "ai-triage:fix-requested" in names
```

- [ ] **Step 3: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_tier2.py -q`
Expected: FAIL — `ModuleNotFoundError: triage_verse.tier2`.

- [ ] **Step 4: Implement `src/triage_verse/tier2.py`**

```python
"""Tier 2: mark an issue for an AI draft-PR fix attempt."""

from __future__ import annotations

from typing import Callable

LABEL = "ai-triage:fix-requested"


def request_fix(repo: str, number: int, *, run_gh: Callable[..., str], label: str = LABEL) -> None:
    run_gh(["issue", "edit", str(number), "--repo", repo, "--add-label", label])
```

- [ ] **Step 5: Wire CLI `tier2`** in `cli.py`

```python
def _cmd_tier2(args):
    from . import executor, tier2
    ref = executor.parse_issue_ref(args.issue, default_repo="")
    if ref is None:
        print(f"error: cannot parse issue ref {args.issue!r}")
        return 1
    tier2.request_fix(ref[0], ref[1], run_gh=gh.run_gh)
    print(f"labeled {ref[0]}#{ref[1]} with {tier2.LABEL}")
    print(f"kick off the fix: gh workflow run tier2-fix.yml -f issue={args.issue}"
          f" -f model={args.model}")
    return 0
```

Subparser `tier2`: positional `issue`, `--model` (choices `sonnet`,`opus`, default `sonnet`).

- [ ] **Step 6: Run to verify pass + yaml validation**

Run: `uv run pytest tests/triage_verse/test_tier2.py -q && make validate-yaml`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add .github/triage/labels.yaml src/triage_verse/tier2.py src/triage_verse/cli.py tests/triage_verse/test_tier2.py
git commit -m "feat(tier2): ai-triage:fix-requested label + tier2 CLI"
```

---

### Task 9: Review-app "Request AI fix" drawer button

**Files:**
- Modify: `src/triage_verse/review_app/app.py`
- Test: `tests/triage_verse/test_review_app_tier2.py`

**Interfaces:**
- Consumes: `tier2.request_fix`, `tier2.LABEL`.
- Produces: a drawer button that, on click, calls `tier2.request_fix(repo, number, run_gh=gh.run_gh)` for the currently-open issue and shows a confirmation notification. Because the app's server logic is Shiny-reactive, the *testable unit* is a small helper `app_tier2_label(repo, number, run_gh)` that the button handler calls; test that helper.

- [ ] **Step 1: Write the failing test**

```python
# tests/triage_verse/test_review_app_tier2.py
"""The review app's Tier-2 label helper."""

from triage_verse.review_app import app as review_app


def test_app_tier2_label_calls_request_fix():
    calls = []
    review_app.app_tier2_label("o/r", 5, run_gh=lambda args, **k: calls.append(args) or "")
    assert calls[0] == ["issue", "edit", "5", "--repo", "o/r",
                        "--add-label", "ai-triage:fix-requested"]
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_review_app_tier2.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'app_tier2_label'`.

- [ ] **Step 3: Implement in `src/triage_verse/review_app/app.py`**

Add near the imports: `from triage_verse import tier2` and `from triage_verse import gh`. Add the helper:

```python
def app_tier2_label(repo: str, number: int, *, run_gh=gh.run_gh) -> None:
    """Apply the Tier-2 fix-requested label to an issue (used by the drawer button)."""
    tier2.request_fix(repo, number, run_gh=run_gh)
```

In the drawer UI (where the issue/PR is rendered — find `_drawer_meta_line`/the drawer render block), add a `ui.input_action_button("request_ai_fix", "Request AI fix", ...)`. In the drawer server logic, add an effect on that button that reads the open item's repo/number and calls `app_tier2_label(...)`, then `ui.notification_show(f"Requested AI fix for {repo}#{number}")`. Follow the existing button/effect pattern already in the file (e.g. how the drawer's approve/reject buttons are wired).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_review_app_tier2.py -q`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/review_app/app.py tests/triage_verse/test_review_app_tier2.py
git commit -m "feat(review-app): Request AI fix drawer button (Tier 2)"
```

---

### Task 10: `tier2-fix.yml` workflow (dormant, guarded)

**Files:**
- Create: `.github/workflows/tier2-fix.yml`
- Modify: `tests/triage_verse/test_workflows.py` (append)

**Interfaces:** none (YAML + guard assertions).

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_tier2_fix_is_dispatch_only_with_issue_input():
    doc, text = _load("tier2-fix.yml")
    triggers = doc.get(True, doc.get("on"))
    assert "workflow_dispatch" in triggers
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "issue" in inputs and inputs["issue"]["required"] is True
    assert "model" in inputs
    active_cron = [ln for ln in text.splitlines()
                   if "cron:" in ln and not ln.strip().startswith("#")]
    assert active_cron == []


def test_tier2_fix_guards_label_and_weekly_cap():
    _, text = _load("tier2-fix.yml")
    assert "ai-triage:fix-requested" in text  # label guard present
    assert "gh run list" in text  # weekly-cap guard present
    assert "--draft" in text  # PR opened as draft
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_workflows.py -q`
Expected: FAIL — `FileNotFoundError: tier2-fix.yml`.

- [ ] **Step 3: Write `.github/workflows/tier2-fix.yml`**

```yaml
name: Tier 2 draft-PR fix

# DORMANT: manual dispatch only. Never auto-merges; always opens a DRAFT PR.
on:
  workflow_dispatch:
    inputs:
      issue:
        description: "Target issue as owner/name#N"
        required: true
      model:
        description: "Claude model"
        required: false
        default: sonnet
        type: choice
        options: [sonnet, opus]

permissions:
  contents: read

jobs:
  fix:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Guard - label present and issue open
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          repo="${{ github.event.inputs.issue }}"; repo="${repo%%#*}"
          num="${{ github.event.inputs.issue }}"; num="${num##*#}"
          state=$(gh issue view "$num" --repo "$repo" --json state,labels \
            --jq '.state + " " + ([.labels[].name] | join(","))')
          echo "$state" | grep -q "ai-triage:fix-requested" \
            || { echo "issue lacks ai-triage:fix-requested label"; exit 1; }
          echo "$state" | grep -qi "^OPEN" || { echo "issue not open"; exit 1; }
      - name: Guard - weekly cap
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          since=$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)
          count=$(gh run list --workflow tier2-fix.yml --status success \
            --created ">=$since" --json databaseId --jq 'length')
          if [ "$count" -ge 10 ]; then echo "weekly Tier 2 cap reached"; exit 1; fi
      - name: Mint installation token
        run: node .github/triage/scripts/create-github-app-token-map.mjs
        env:
          APP_ID: ${{ secrets.TRIAGE_APP_ID }}
          APP_PRIVATE_KEY: ${{ secrets.TRIAGE_APP_PRIVATE_KEY }}
      - name: Install Claude CLI
        run: npm install -g @anthropic-ai/claude-code
      - name: Attempt fix and open draft PR
        env:
          GH_TOKEN: ${{ github.token }}
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
        run: |
          echo "Fix session runs here; opens a DRAFT PR referencing the issue."
          echo "gh pr create --draft --title ... --body ... referencing the issue"
      - name: Summary
        run: echo "Tier 2 fix attempt for ${{ github.event.inputs.issue }}" >> "$GITHUB_STEP_SUMMARY"
```

Note: the fix-session step is intentionally a scaffold echoing the exact `gh pr create --draft` command shape — the token router + Claude Code invocation are wired to real secrets when the workflow is activated. The dormancy tests assert the guards and `--draft` are present.

- [ ] **Step 4: Run to verify pass + validation**

Run: `uv run pytest tests/triage_verse/test_workflows.py -q && make validate-yaml`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/tier2-fix.yml tests/triage_verse/test_workflows.py
git commit -m "feat(ci): dormant Tier 2 draft-PR workflow with label + weekly-cap guards"
```

---

### Task 11: Autonomy — precision, promotion, demotion (`autonomy.py`)

**Files:**
- Create: `src/triage_verse/autonomy.py`
- Test: `tests/triage_verse/test_autonomy.py`

**Interfaces:**
- Consumes: `review_queue.iter_jsonl_records`; `config.AutonomyConfig`.
- Produces:
  - `autonomy.ELIGIBLE = ("add-label", "set-priority")`.
  - `autonomy.category_precision(decisions: list[dict]) -> dict[str, dict]` — per action category over the most recent consecutive human-reviewed decisions: `{action: {"reviewed": int, "precision": float}}` (approved/edited = success, rejected = failure, skipped excluded, auto-approved excluded).
  - `autonomy.evaluate(decisions, results, cfg: AutonomyConfig) -> dict[str, dict]` — for eligible categories, `{action: {"reviewed", "precision", "promote": bool, "audit_failures": int}}`; `promote` True iff `reviewed >= cfg.min_decisions and precision >= cfg.min_precision`. `results` supplies audit rejections/reopens folded into precision.
  - `autonomy.render_config(evaluated, cfg, *, today: str) -> dict` — the `config/autonomy.yaml` document (`{"promoted": {action: {promoted_at, confidence_floor}}}`) for currently-promoted categories.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_autonomy.py
"""Graduated-autonomy precision, promotion, demotion."""

from triage_verse import autonomy, config


CFG = config.AutonomyConfig(min_decisions=4, min_precision=0.75,
                            confidence_floor=0.9, audit_rate=0.10)


def _d(action, verdict):
    return {"action": action, "verdict": verdict}


def test_category_precision_counts_success_and_failure():
    decisions = [_d("add-label", "approved"), _d("add-label", "approved"),
                 _d("add-label", "rejected"), _d("add-label", "skipped")]
    prec = autonomy.category_precision(decisions)
    assert prec["add-label"]["reviewed"] == 3  # skipped excluded
    assert abs(prec["add-label"]["precision"] - 2 / 3) < 1e-9


def test_promote_only_when_thresholds_met():
    good = [_d("add-label", "approved")] * 4
    bad = [_d("set-priority", "approved")] * 2 + [_d("set-priority", "rejected")] * 2
    ev = autonomy.evaluate(good + bad, [], CFG)
    assert ev["add-label"]["promote"] is True
    assert ev["set-priority"]["promote"] is False  # 0.5 < 0.75


def test_close_never_eligible():
    decisions = [_d("close", "approved")] * 100
    ev = autonomy.evaluate(decisions, [], CFG)
    assert "close" not in ev


def test_audit_rejection_demotes_via_precision():
    decisions = [_d("add-label", "approved")] * 4
    # one audit rejection recorded in results counts as a failure
    results = [{"action": "add-label", "audit_verdict": "rejected"}]
    ev = autonomy.evaluate(decisions, results, CFG)
    # 4 success + 1 failure = 0.8 >= 0.75 still promotes; add a second failure:
    results += [{"action": "add-label", "audit_verdict": "rejected"}]
    ev2 = autonomy.evaluate(decisions, results, CFG)
    assert ev["add-label"]["promote"] is True
    assert ev2["add-label"]["promote"] is False  # 4/6 = 0.667 < 0.75


def test_render_config_lists_promoted_only():
    good = [_d("add-label", "approved")] * 4
    ev = autonomy.evaluate(good, [], CFG)
    doc = autonomy.render_config(ev, CFG, today="2026-08-01")
    assert doc == {"promoted": {"add-label": {"promoted_at": "2026-08-01",
                                              "confidence_floor": 0.9}}}
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_autonomy.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/triage_verse/autonomy.py`**

```python
"""Graduated autonomy: per-category precision, promotion, demotion."""

from __future__ import annotations

ELIGIBLE = ("add-label", "set-priority")
_SUCCESS = {"approved", "edited"}
_FAILURE = {"rejected"}


def category_precision(decisions: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for action in {d.get("action") for d in decisions}:
        judged = [
            d for d in decisions
            if d.get("action") == action
            and d.get("verdict") in _SUCCESS | _FAILURE
            and d.get("decided_by") != "autonomy"
        ]
        if not judged:
            continue
        ok = sum(1 for d in judged if d["verdict"] in _SUCCESS)
        out[action] = {"reviewed": len(judged), "precision": ok / len(judged)}
    return out


def evaluate(decisions: list[dict], results: list[dict], cfg) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for action in ELIGIBLE:
        judged = [
            d for d in decisions
            if d.get("action") == action
            and d.get("verdict") in _SUCCESS | _FAILURE
            and d.get("decided_by") != "autonomy"
        ]
        audit_failures = sum(
            1 for r in results
            if r.get("action") == action and r.get("audit_verdict") == "rejected"
        )
        total = len(judged) + audit_failures
        if total == 0:
            continue
        ok = sum(1 for d in judged if d["verdict"] in _SUCCESS)
        precision = ok / total
        out[action] = {
            "reviewed": len(judged),
            "precision": precision,
            "audit_failures": audit_failures,
            "promote": len(judged) >= cfg.min_decisions and precision >= cfg.min_precision,
        }
    return out


def render_config(evaluated: dict[str, dict], cfg, *, today: str) -> dict:
    promoted = {
        action: {"promoted_at": today, "confidence_floor": cfg.confidence_floor}
        for action, ev in evaluated.items()
        if ev.get("promote")
    }
    return {"promoted": promoted}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_autonomy.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/autonomy.py tests/triage_verse/test_autonomy.py
git commit -m "feat(autonomy): per-category precision, promotion, demotion"
```

---

### Task 12: `autonomy status` CLI + `config/autonomy.yaml`

**Files:**
- Modify: `src/triage_verse/cli.py`
- Create: `config/autonomy.yaml`
- Test: `tests/triage_verse/test_cli_autonomy.py`

**Interfaces:**
- Consumes: `autonomy.evaluate`, `autonomy.render_config`; `config.load_models_config`; `review_queue.iter_jsonl_records`.
- Produces: CLI `triage-verse autonomy status [--write]`. Reads decisions + results dirs, prints per-category table, and with `--write` dumps `config/autonomy.yaml` via `yaml.safe_dump`.

- [ ] **Step 1: Create `config/autonomy.yaml`** (empty promoted set — nothing auto-applies yet)

```yaml
# Written by `triage-verse autonomy status --write`. Categories listed here
# auto-apply above their confidence_floor via `triage-verse execute --auto`.
# Empty = fully human-gated (v1 default).
promoted: {}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/triage_verse/test_cli_autonomy.py
"""autonomy status CLI."""

import pathlib

import yaml

from triage_verse import cli, jsonl_log


def test_autonomy_status_write_promotes(tmp_path, monkeypatch, capsys):
    dec = tmp_path / "decisions"
    jsonl_log.append_weekly(
        [{"id": f"d{i}", "action": "add-label", "verdict": "approved"} for i in range(200)],
        dec,
    )
    out_yaml = tmp_path / "autonomy.yaml"
    rc = cli.main([
        "autonomy", "status", "--write",
        "--decisions-dir", str(dec), "--results-dir", str(tmp_path / "results"),
        "--out", str(out_yaml), "--models-config", "config/models.yaml",
    ])
    assert rc == 0
    doc = yaml.safe_load(out_yaml.read_text(encoding="utf-8"))
    assert "add-label" in doc["promoted"]
    assert "add-label" in capsys.readouterr().out
```

- [ ] **Step 3: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_cli_autonomy.py -q`
Expected: FAIL — argparse `invalid choice: 'autonomy'`.

- [ ] **Step 4: Wire CLI in `cli.py`**

```python
def _cmd_autonomy_status(args):
    from . import autonomy, review_queue
    import yaml
    cfg = config.load_models_config(args.models_config).autonomy
    decisions = review_queue.iter_jsonl_records(args.decisions_dir)
    results = review_queue.iter_jsonl_records(args.results_dir)
    ev = autonomy.evaluate(decisions, results, cfg)
    for action, e in sorted(ev.items()):
        flag = "PROMOTE" if e["promote"] else "hold"
        print(f"{action}: reviewed={e['reviewed']} precision={e['precision']:.3f}"
              f" audit_fail={e['audit_failures']} -> {flag}")
    if not ev:
        print("no eligible categories with reviewed decisions yet")
    if args.write:
        doc = autonomy.render_config(ev, cfg, today=_state_now()[:10])
        pathlib.Path(args.out).write_text(yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0
```

Subparser: `autonomy` → `status` with `--decisions-dir` (default `.data/decisions`), `--results-dir` (default `.data/results`), `--models-config` (default `DEFAULT_MODELS`), `--out` (default `config/autonomy.yaml`), `--write`.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_cli_autonomy.py -q`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add config/autonomy.yaml src/triage_verse/cli.py tests/triage_verse/test_cli_autonomy.py
git commit -m "feat(autonomy): autonomy status CLI + config/autonomy.yaml"
```

---

### Task 13: `execute --auto` — synthetic decisions + audit sampling

**Files:**
- Modify: `src/triage_verse/executor.py`
- Modify: `src/triage_verse/cli.py`
- Test: `tests/triage_verse/test_executor_auto.py`

**Interfaces:**
- Consumes: `autonomy` promoted config; Plan 4 `execute` machinery; `review_queue.load_undecided`/`iter_jsonl_records`.
- Produces:
  - `executor.load_autonomy(path) -> dict[str, dict]` — the `promoted` map from `config/autonomy.yaml` (`{}` if absent).
  - `executor.select_auto(proposals, decided_ids, promoted, *, audit_rate) -> list[dict]` — undecided proposals whose action is promoted and `confidence >= floor`; each returned dict includes `"audit": bool` (deterministic 10% sample by stable hash of proposal id).
  - `execute(..., auto=False, autonomy_path="config/autonomy.yaml", proposals_dir=...)`: when `auto=True and apply=True`, before the normal decision loop it writes synthetic decision records (`verdict="auto-approved"`, `decided_by="autonomy"`, `audit` flag copied) to the decisions dir so they're picked up by the existing selection path.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_executor_auto.py
"""execute --auto: synthetic decisions + deterministic audit sampling."""

import importlib.util
import pathlib

from triage_verse import db, executor, jsonl_log, review_queue

_spec = importlib.util.spec_from_file_location(
    "fake_gh", pathlib.Path(__file__).with_name("fake_gh.py"))
fake_gh = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(fake_gh)
FakeGh = fake_gh.FakeGh

UPDATED = "2026-07-01T00:00:00Z"


def _proposal(pid, action, params, conf, issue=1):
    return {"id": pid, "repo": "o/r", "issue": issue, "issue_updated_at": UPDATED,
            "run_id": "r", "model": "m", "confidence": conf, "evidence": [],
            "action": action, "params": params, "rationale": ""}


def test_select_auto_filters_by_promotion_and_floor():
    proposals = [
        _proposal("p1", "add-label", {"label": "regression"}, 0.95),
        _proposal("p2", "add-label", {"label": "regression"}, 0.5),   # below floor
        _proposal("p3", "close", {"reason": "fixed"}, 0.99, issue=2),  # not eligible
    ]
    promoted = {"add-label": {"confidence_floor": 0.9}}
    picked = executor.select_auto(proposals, set(), promoted, audit_rate=0.0)
    assert [p["id"] for p in picked] == ["p1"]
    assert picked[0]["audit"] is False


def test_auto_writes_synthetic_decisions_then_executes(tmp_path):
    dirs = {"decisions_dir": tmp_path / "dec", "proposals_dir": tmp_path / "prop",
            "results_dir": tmp_path / "res"}
    jsonl_log.append_weekly([_proposal("p1", "add-label", {"label": "regression"}, 0.95)],
                            dirs["proposals_dir"])
    autonomy_path = tmp_path / "autonomy.yaml"
    autonomy_path.write_text("promoted:\n  add-label: {confidence_floor: 0.9}\n",
                             encoding="utf-8")
    con = db.connect(":memory:")
    con.execute("INSERT INTO issues (repo, number, title, state, updated_at, created_at,"
                " labels_json) VALUES ('o/r',1,'t','OPEN',?,?, '[]')", (UPDATED, UPDATED))
    gh = FakeGh({("o/r", 1): {"labels": [], "state": "open", "state_reason": None,
                              "updated_at": UPDATED, "node_id": "N1"}})
    summary = executor.execute(con, run_gh=gh, apply=True, auto=True,
                               autonomy_path=str(autonomy_path), pace=lambda s: None,
                               log=lambda *a: None, **dirs)
    assert summary["counts"]["applied"] == 1
    dec = review_queue.iter_jsonl_records(dirs["decisions_dir"])
    assert dec[0]["verdict"] == "auto-approved" and dec[0]["decided_by"] == "autonomy"
    assert gh.issues[("o/r", 1)]["labels"] == ["regression"]


def test_audit_sampling_is_deterministic():
    proposals = [_proposal(f"p{i}", "add-label", {"label": "regression"}, 0.95)
                 for i in range(100)]
    promoted = {"add-label": {"confidence_floor": 0.9}}
    a = executor.select_auto(proposals, set(), promoted, audit_rate=0.10)
    b = executor.select_auto(proposals, set(), promoted, audit_rate=0.10)
    assert [p["audit"] for p in a] == [p["audit"] for p in b]
    flagged = sum(1 for p in a if p["audit"])
    assert 3 <= flagged <= 20  # ~10% of 100, deterministic band
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_executor_auto.py -q`
Expected: FAIL — `AttributeError: module 'triage_verse.executor' has no attribute 'select_auto'`.

- [ ] **Step 3: Implement in `src/triage_verse/executor.py`** (append + extend `execute`)

Add imports if missing: `import hashlib`, `import pathlib`. Add:

```python
AUTO_ELIGIBLE = ("add-label", "set-priority")


def load_autonomy(path) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    import yaml
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc.get("promoted") or {}


def _audit_flag(proposal_id: str, audit_rate: float) -> bool:
    if audit_rate <= 0:
        return False
    h = int(hashlib.sha256(proposal_id.encode()).hexdigest()[:8], 16)
    return (h % 100) < round(audit_rate * 100)


def select_auto(proposals, decided_ids, promoted, *, audit_rate: float) -> list[dict]:
    out = []
    for p in proposals:
        if p.get("id") in decided_ids:
            continue
        action = p.get("action")
        if action not in AUTO_ELIGIBLE or action not in promoted:
            continue
        floor = promoted[action].get("confidence_floor", 1.0)
        if (p.get("confidence") or 0.0) < floor:
            continue
        out.append({**p, "audit": _audit_flag(p["id"], audit_rate)})
    return out
```

In `execute`, add params `auto: bool = False`, `autonomy_path="config/autonomy.yaml"`, `audit_rate: float = 0.10`. Near the top, after loading `results`/decisions but before selecting, when `auto and apply`:

```python
    if auto and apply:
        promoted = load_autonomy(autonomy_path)
        all_props = review_queue.iter_jsonl_records(proposals_dir)
        decided = {d.get("proposal_id") for d in review_queue.iter_jsonl_records(decisions_dir)}
        auto_props = select_auto(all_props, decided, promoted, audit_rate=audit_rate)
        synthetic = []
        for p in auto_props:
            rec = {
                "id": uuid.uuid4().hex, "proposal_id": p["id"], "repo": p["repo"],
                "issue": p["issue"], "action": p["action"], "params": p["params"],
                "verdict": "auto-approved", "decided_by": "autonomy",
                "confidence": p.get("confidence"), "audit": p["audit"],
                "decided_at": _now(),
            }
            synthetic.append(rec)
        if synthetic:
            jsonl_log.append_weekly(synthetic, decisions_dir)
```

Then let the existing selection/execution proceed (it will now pick up the synthetic `auto-approved` decisions — add `"auto-approved"` to `EXECUTABLE_VERDICTS`). Update:

```python
EXECUTABLE_VERDICTS = frozenset({"approved", "edited", "auto-approved"})
```

- [ ] **Step 4: Wire `--auto` into the CLI** (`_cmd_execute`, add flag)

Add `--auto` (`action="store_true"`) to the `execute` subparser and pass `auto=args.auto` and `autonomy_path` (default `config/autonomy.yaml`, add `--autonomy` arg) into `executor.execute`.

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/triage_verse/test_executor_auto.py tests/triage_verse/test_executor_execute.py tests/triage_verse/test_executor_select.py -q`
Expected: all pass (existing execute/select tests unaffected — `auto` defaults False).

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/executor.py src/triage_verse/cli.py tests/triage_verse/test_executor_auto.py
git commit -m "feat(autonomy): execute --auto via synthetic decisions + audit sampling"
```

---

### Task 14: Review-app Audit section + docs + full check

**Files:**
- Modify: `src/triage_verse/review_app/app.py`
- Modify: `README.md`
- Test: `tests/triage_verse/test_review_app_audit.py`

**Interfaces:**
- Consumes: results log records with `decided_by == "autonomy"` joined to decisions with `audit == True`.
- Produces: `review_app.app_audit_items(decisions_dir, results_dir) -> list[dict]` — executed, audit-flagged auto-decisions needing confirmation: `[{repo, issue, action, params, batch_id, result_id}]`. The Audit UI section lists them with Confirm/Reject; Reject writes a `rejected` decision (a precision failure) and prints the undo command. Test the pure helper.

- [ ] **Step 1: Write the failing test**

```python
# tests/triage_verse/test_review_app_audit.py
"""Audit-section helper: list executed auto-decisions flagged for audit."""

from triage_verse import jsonl_log
from triage_verse.review_app import app as review_app


def test_app_audit_items_lists_executed_audit_flagged(tmp_path):
    dec = tmp_path / "decisions"
    res = tmp_path / "results"
    jsonl_log.append_weekly([
        {"id": "d1", "proposal_id": "p1", "repo": "o/r", "issue": 1,
         "action": "add-label", "params": {"label": "regression"},
         "verdict": "auto-approved", "decided_by": "autonomy", "audit": True},
        {"id": "d2", "proposal_id": "p2", "repo": "o/r", "issue": 2,
         "action": "add-label", "params": {"label": "regression"},
         "verdict": "auto-approved", "decided_by": "autonomy", "audit": False},
    ], dec)
    jsonl_log.append_weekly([
        {"id": "r1", "decision_id": "d1", "batch_id": "b1", "repo": "o/r",
         "issue": 1, "action": "add-label", "status": "applied"},
        {"id": "r2", "decision_id": "d2", "batch_id": "b1", "repo": "o/r",
         "issue": 2, "action": "add-label", "status": "applied"},
    ], res)
    items = review_app.app_audit_items(dec, res)
    assert len(items) == 1
    assert items[0]["issue"] == 1 and items[0]["batch_id"] == "b1"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/triage_verse/test_review_app_audit.py -q`
Expected: FAIL — `AttributeError: ... 'app_audit_items'`.

- [ ] **Step 3: Implement the helper in `app.py`**

```python
def app_audit_items(decisions_dir, results_dir) -> list[dict]:
    """Executed auto-approved decisions flagged for spot audit, needing confirm/reject."""
    from triage_verse import review_queue
    audit_decisions = {
        d["id"]: d
        for d in review_queue.iter_jsonl_records(decisions_dir)
        if d.get("decided_by") == "autonomy" and d.get("audit") is True
    }
    out = []
    for r in review_queue.iter_jsonl_records(results_dir):
        d = audit_decisions.get(r.get("decision_id"))
        if d is None or r.get("status") != "applied":
            continue
        out.append({
            "repo": r["repo"], "issue": r["issue"], "action": r["action"],
            "params": d.get("params"), "batch_id": r.get("batch_id"),
            "result_id": r.get("id"),
        })
    return out
```

Add a lightweight **Audit** nav panel/section that renders `app_audit_items(DECISIONS_DIR, RESULTS_DIR)` with Confirm (dismiss) and Reject buttons; Reject writes a `rejected` decision for that proposal via `decisions.write` and shows the `triage-verse undo --batch <batch_id> --issue <repo>#<issue> --apply` command. Follow the existing dashboard/queue panel wiring pattern already in the file.

- [ ] **Step 4: Update `README.md`**

Under the Executor section (or a new "Steady state & automation" heading), add short entries for: `state pull`/`push` (triage-state bus), `steady-state` (the loop; note the workflow ships dormant), `tier1` (already-fixed proposals, capped/day), `tier2 owner/repo#N` (label an issue for an AI draft-PR attempt), `autonomy status [--write]` (precision → `config/autonomy.yaml`), and `execute --auto` (auto-apply promoted categories with spot audits). Match the existing README format.

- [ ] **Step 5: Run to verify pass, then full gate**

Run: `uv run pytest tests/triage_verse/test_review_app_audit.py -q`
Expected: pass.

Run: `make check`
Expected: ruff, pyright, pytest, validate-yaml, compile-scripts, node tests all green. Fix anything flagged (likely: ruff format on new files → `uv run ruff format <files>`; pyright wants `dict[str, Any]` annotations on a few helpers; unused imports). Re-run `make check` until green.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/review_app/app.py README.md tests/triage_verse/test_review_app_audit.py
git commit -m "feat(review-app): autonomy audit section + docs"
```

---

## Plan self-review (done at write time)

- **Spec coverage:** 5a state bus (T2 merge core, T3 push/pull+cursors+CLI), steady-state loop (T4) + dormant workflow (T5); 5b Tier 1 selection (T6) + session/cap/breaker/CLI (T7); 5c label+CLI (T8), review-app button (T9), dormant guarded workflow (T10); 5d precision/promotion/demotion (T11), status CLI + autonomy.yaml (T12), execute --auto + audit sampling (T13), audit review section (T14); config block (T1). Testing rows from the spec map: union-merge props → T2; push/pull round trip + no-op → T3; steady-state orchestration + mid-loop failure → T4; tier1 selection/parsing/cap/breaker → T6/T7; tier2 label + allowlist regression → T8; autonomy boundaries/demotion/eligibility → T11; --auto round trip + audit determinism → T13; workflow dormancy → T5/T10.
- **Global constraints enforced in tasks:** no-issue-write CI perms (T5 test), dispatch-only + no active cron (T5/T10 tests), label excluded from allowed_safe_output (T8 test), only add-label/set-priority eligible (T11 test), promotion is an explicit file write (T12), dry-run default preserved (T13 auto only acts under apply).
- **Type consistency:** `run_git(args, *, cwd=None)` shape shared T3; `select_candidates(con, repos, *, proposals_dir, limit)` T6 consumed by `tier1.run` T7; `tier2.request_fix(repo, number, *, run_gh, label=LABEL)` T8 reused T9; `autonomy.evaluate/render_config` T11 consumed T12; `select_auto`/synthetic `auto-approved` + `EXECUTABLE_VERDICTS` extension T13 consumed by Plan-4 selection; `app_audit_items` reads the `audit`/`decided_by` fields written in T13.
- **Deferred (spec out-of-scope):** activating cron, CI issue mutation, Tier 2 self-selection/auto-merge, multi-tenancy — no tasks, intentional.
