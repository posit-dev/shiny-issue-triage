# CLI `--json` Output Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global `--json` flag to the `triage-verse` CLI so every command emits a single structured JSON envelope on stdout, with human logs on stderr and exit codes preserved.

**Architecture:** A small `Output` helper in `cli.py` centralizes all output routing. In JSON mode it prints a uniform envelope `{command, ok, exit_code, data|error}` to stdout and sends logs to stderr; in human mode it prints prose to stdout exactly as today. `--json` is parsed in either position (before or after the subcommand) via a shared `argparse` parent parser plus a top-level definition. `main()` wraps command dispatch so unexpected exceptions become `ok:false` envelopes under `--json`.

**Tech Stack:** Python 3, `argparse`, `json`, `pytest`. Managed with `uv`. Gate: `make py-check` (ruff format + lint, pyright, pytest).

## Global Constraints

- **Envelope shape (verbatim):** success → `{"command": <str>, "ok": true, "exit_code": <int>, "data": <obj|array>}`; failure → `{"command": <str>, "ok": false, "exit_code": <int>, "error": <str>}`. One JSON object per invocation, printed as a single line on stdout.
- **`ok` semantics:** `ok` is `true` iff the command ran to completion. Negative domain results (count mismatch, execution errors) are `ok:true` with a non-zero `exit_code`. Only bad input and unexpected exceptions are `ok:false`.
- **Logs → stderr in JSON mode.** stdout carries only the envelope. Never suppress logs; redirect them. The codebase's logging-visibility convention stays intact.
- **`--json` works in either position:** `triage-verse --json sync` ≡ `triage-verse sync --json`, including nested subcommands (`analytics export`).
- **Human mode is unchanged.** The refactor must not alter any non-JSON stdout. Existing `tests/triage_verse/test_cli*.py` must keep passing.
- Argparse dest for the flag is `json_mode` (not `json`, to avoid confusion with the `json` module).
- Run `make py-check` before considering any task done.

---

### Task 1: `Output` helper

**Files:**
- Modify: `src/triage_verse/cli.py` (add `import json`; add `Output` class near the top, after the constants block ~line 26)
- Test: `tests/triage_verse/test_cli_json.py` (create)

**Interfaces:**
- Produces: `Output(command: str, json_mode: bool)` with methods:
  - `log(msg: str) -> None`
  - `emit(data, human: str, exit_code: int = 0) -> int`
  - `fail(message: str, exit_code: int = 1) -> int`

- [ ] **Step 1: Write the failing tests**

Create `tests/triage_verse/test_cli_json.py`:

```python
import json

from triage_verse.cli import Output


def test_emit_json_envelope_success(capsys):
    rc = Output("sync", json_mode=True).emit({"issues": 4}, human="synced", exit_code=0)
    assert rc == 0
    out = capsys.readouterr()
    assert out.err == ""
    doc = json.loads(out.out)
    assert doc == {"command": "sync", "ok": True, "exit_code": 0, "data": {"issues": 4}}


def test_emit_human_prints_prose(capsys):
    rc = Output("sync", json_mode=False).emit({"issues": 4}, human="synced 4", exit_code=0)
    assert rc == 0
    out = capsys.readouterr()
    assert out.out.strip() == "synced 4"


def test_emit_preserves_nonzero_exit_code_with_ok_true(capsys):
    rc = Output("verify-counts", json_mode=True).emit(
        {"reconciled": False}, human="mismatch", exit_code=1
    )
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["exit_code"] == 1


def test_fail_json_envelope(capsys):
    rc = Output("sync", json_mode=True).fail("bad repo", exit_code=1)
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"command": "sync", "ok": False, "exit_code": 1, "error": "bad repo"}


def test_fail_human_prints_to_stderr(capsys):
    rc = Output("sync", json_mode=False).fail("bad repo")
    assert rc == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == "error: bad repo"


def test_log_routes_to_stderr_in_json_mode(capsys):
    Output("sync", json_mode=True).log("progress")
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == "progress"


def test_log_routes_to_stdout_in_human_mode(capsys):
    Output("sync", json_mode=False).log("progress")
    out = capsys.readouterr()
    assert out.out.strip() == "progress"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -v`
Expected: FAIL with `ImportError: cannot import name 'Output'`

- [ ] **Step 3: Implement `Output`**

In `src/triage_verse/cli.py`, add `import json` to the imports (alphabetically, after `import io`), then add after the constants (after `DEFAULT_PROPOSALS = ...`, ~line 26):

```python
class Output:
    """Routes command output for human (prose to stdout) or --json mode
    (one envelope to stdout, logs to stderr)."""

    def __init__(self, command: str, json_mode: bool) -> None:
        self.command = command
        self.json_mode = json_mode

    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr if self.json_mode else sys.stdout)

    def emit(self, data: object, human: str, exit_code: int = 0) -> int:
        if self.json_mode:
            print(
                json.dumps(
                    {
                        "command": self.command,
                        "ok": True,
                        "exit_code": exit_code,
                        "data": data,
                    }
                )
            )
        else:
            print(human)
        return exit_code

    def fail(self, message: str, exit_code: int = 1) -> int:
        if self.json_mode:
            print(
                json.dumps(
                    {
                        "command": self.command,
                        "ok": False,
                        "exit_code": exit_code,
                        "error": message,
                    }
                )
            )
        else:
            print(f"error: {message}", file=sys.stderr)
        return exit_code
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_json.py
git commit -m "feat(cli): add Output helper for --json envelope (#42)"
```

---

### Task 2: Wire `--json` parsing + `main()` exception wrapper + migrate `sync`

**Files:**
- Modify: `src/triage_verse/cli.py` (`build_parser`, `main`, `_cmd_sync`)
- Test: `tests/triage_verse/test_cli_json.py` (append)

**Interfaces:**
- Consumes: `Output` from Task 1.
- Produces:
  - A shared `common` parent parser carrying `--json` (dest `json_mode`, `default=argparse.SUPPRESS`).
  - Every subparser created with `parents=[common]`; every leaf parser sets `cmdname` via `set_defaults` (e.g. `cmdname="sync"`, `cmdname="analytics export"`).
  - `main()` builds `Output(args.cmdname, args.json_mode)`, attaches it as `args._out`, and wraps dispatch: on `Exception` in JSON mode it prints an `ok:false` envelope and returns 1; in human mode it re-raises.
  - Handlers read `args._out`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_cli_json.py`:

```python
from triage_verse import cli
from triage_verse import sync as sync_mod


def _repos_cfg(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n")
    return cfg


def test_sync_json_envelope(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    monkeypatch.setattr(
        sync_mod,
        "sync_all",
        lambda con, repos, *, full, log: {"repos": 1, "issues": 2, "prs": 0, "comments": 3},
    )
    rc = cli.main(["sync", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "sync"
    assert doc["ok"] is True
    assert doc["data"] == {"repos": 1, "issues": 2, "prs": 0, "comments": 3}


def test_json_flag_accepted_before_subcommand(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    monkeypatch.setattr(
        sync_mod,
        "sync_all",
        lambda con, repos, *, full, log: {"repos": 1, "issues": 0, "prs": 0, "comments": 0},
    )
    rc = cli.main(["--json", "sync", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["command"] == "sync"


def test_sync_logs_go_to_stderr_in_json_mode(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)

    def fake_sync_all(con, repos, *, full, log):
        log("mirroring rstudio/shiny")
        return {"repos": 1, "issues": 0, "prs": 0, "comments": 0}

    monkeypatch.setattr(sync_mod, "sync_all", fake_sync_all)
    cli.main(["sync", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)])
    out = capsys.readouterr()
    assert "mirroring" in out.err
    json.loads(out.out)  # stdout is exactly the envelope, still parseable


def test_sync_unknown_repo_json_error(tmp_path, capsys):
    cfg = _repos_cfg(tmp_path)
    rc = cli.main(
        ["sync", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg), "--repo", "rstudio/nope"]
    )
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and doc["exit_code"] == 1 and "nope" in doc["error"]


def test_unexpected_exception_becomes_json_envelope(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)

    def boom(con, repos, *, full, log):
        raise RuntimeError("network died")

    monkeypatch.setattr(sync_mod, "sync_all", boom)
    rc = cli.main(["sync", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)])
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"command": "sync", "ok": False, "exit_code": 1, "error": "network died"}


def test_unexpected_exception_reraises_in_human_mode(tmp_path, monkeypatch):
    import pytest

    cfg = _repos_cfg(tmp_path)

    def boom(con, repos, *, full, log):
        raise RuntimeError("network died")

    monkeypatch.setattr(sync_mod, "sync_all", boom)
    with pytest.raises(RuntimeError, match="network died"):
        cli.main(["sync", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -v`
Expected: FAIL (unrecognized `--json`, or `AttributeError` on `args._out`).

- [ ] **Step 3: Wire the parser**

In `build_parser()`, immediately after `sub = parser.add_subparsers(...)` add the shared parent and top-level flag:

```python
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        dest="json_mode",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit a single JSON envelope on stdout; logs go to stderr",
    )
    parser.add_argument(
        "--json", dest="json_mode", action="store_true", default=False
    )
```

Then give **every** `sub.add_parser(...)` and every nested `*_sub.add_parser(...)` call `parents=[common]`. For example:

```python
    p_sync = sub.add_parser(
        "sync", help="mirror issues/PRs/comments to SQLite", parents=[common]
    )
```

Apply the same `parents=[common]` to: `snapshot`, `snapshot publish`, `snapshot bootstrap`, `analytics`, `analytics export`, `verify-counts`, `embed`, `analyze`, `analyze-status`, `execute`, `undo`, `state`, `state pull`, `state push`, `tier1`, `tier2`, `steady-state`, `autonomy`, `autonomy status`. (Parent groups like `snapshot`/`analytics`/`state`/`autonomy` should get it too so `--json` works right after the group word.)

Add `cmdname=` to each leaf parser's `set_defaults`:

```python
    p_sync.set_defaults(func=_cmd_sync, cmdname="sync")
    ...
    p_pub.set_defaults(func=_cmd_snapshot_publish, cmdname="snapshot publish")
    p_boot.set_defaults(func=_cmd_snapshot_bootstrap, cmdname="snapshot bootstrap")
    p_exp.set_defaults(func=_cmd_analytics_export, cmdname="analytics export")
    p_ver.set_defaults(func=_cmd_verify_counts, cmdname="verify-counts")
    p_embed.set_defaults(func=_cmd_embed, cmdname="embed")
    p_an.set_defaults(func=_cmd_analyze, cmdname="analyze")
    p_st.set_defaults(func=_cmd_analyze_status, cmdname="analyze-status")
    p_exec.set_defaults(func=_cmd_execute, cmdname="execute")
    p_undo.set_defaults(func=_cmd_undo, cmdname="undo")
    p_pull.set_defaults(func=_cmd_state_pull, cmdname="state pull")
    p_push.set_defaults(func=_cmd_state_push, cmdname="state push")
    p_t1.set_defaults(func=_cmd_tier1, cmdname="tier1")
    p_t2.set_defaults(func=_cmd_tier2, cmdname="tier2")
    p_ss.set_defaults(func=_cmd_steady_state, cmdname="steady-state", repo=None, limit=None, full=False, wait=True)
    p_auto_st.set_defaults(func=_cmd_autonomy_status, cmdname="autonomy status")
```

- [ ] **Step 4: Wire `main()`**

Replace the body of `main()` after `args = build_parser().parse_args(argv)` with:

```python
    out = Output(args.cmdname, bool(getattr(args, "json_mode", False)))
    args._out = out
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - re-raised in human mode
        if out.json_mode:
            print(
                json.dumps(
                    {
                        "command": out.command,
                        "ok": False,
                        "exit_code": 1,
                        "error": str(exc),
                    }
                )
            )
            return 1
        raise
```

- [ ] **Step 5: Migrate `_cmd_sync`**

Replace `_cmd_sync` with:

```python
def _cmd_sync(args: argparse.Namespace) -> int:
    out = args._out
    repos = [r.full for r in config.load_repos(args.config)]
    if args.repo:
        if args.repo not in repos:
            return out.fail(f"{args.repo} is not in {args.config}")
        repos = [args.repo]
    con = _open_db(args.db)
    totals = sync_mod.sync_all(con, repos, full=args.full, log=out.log)
    human = (
        f"synced {totals['repos']} repos: {totals['issues']} issues, "
        f"{totals['prs']} PRs, {totals['comments']} comments"
    )
    return out.emit(totals, human)
```

- [ ] **Step 6: Run the new and existing CLI tests**

Run: `uv run pytest tests/triage_verse/test_cli_json.py tests/triage_verse/test_cli.py -v`
Expected: PASS (all). Note the human-mode error message changed from `error: {repo} is not in {config}` printed to **stdout** to the same text on **stderr** via `out.fail`; `test_cli_sync_unknown_repo_returns_1` only asserts `rc == 1`, so it still passes.

- [ ] **Step 7: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_json.py
git commit -m "feat(cli): parse --json in either position, wrap dispatch, migrate sync (#42)"
```

---

### Task 3: Migrate `verify-counts` (domain-negative) + `analyze-status`

**Files:**
- Modify: `src/triage_verse/cli.py` (`_cmd_verify_counts`, `_cmd_analyze_status`)
- Test: `tests/triage_verse/test_cli_json.py` (append)

**Interfaces:**
- Consumes: `args._out`, `Output` from Tasks 1–2.
- Produces: `verify-counts` data `{reconciled, tolerance, repos:[{repo, mirror, github, diff, ok}]}` with `exit_code=1` on any mismatch; `analyze-status` data `{open_batches, today_spend_usd}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_cli_json.py`:

```python
from triage_verse import verify as verify_mod
from triage_verse import analyze as analyze_mod


def test_verify_counts_mismatch_is_ok_true_exit_1(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    monkeypatch.setattr(
        verify_mod,
        "verify_counts",
        lambda con, repos, *, tolerance: [
            {"repo": "rstudio/shiny", "mirror": 10, "github": 12, "ok": False}
        ],
    )
    rc = cli.main(["verify-counts", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)])
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True
    assert doc["exit_code"] == 1
    assert doc["data"]["reconciled"] is False
    assert doc["data"]["repos"][0]["diff"] == 2


def test_verify_counts_all_ok_exit_0(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    monkeypatch.setattr(
        verify_mod,
        "verify_counts",
        lambda con, repos, *, tolerance: [
            {"repo": "rstudio/shiny", "mirror": 10, "github": 10, "ok": True}
        ],
    )
    rc = cli.main(["verify-counts", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["data"]["reconciled"] is True


def test_analyze_status_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        analyze_mod,
        "analyze_status",
        lambda con: {"open_batches": [], "today_spend_usd": 1.25},
    )
    rc = cli.main(["analyze-status", "--json", "--db", str(tmp_path / "m.sqlite")])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["data"] == {"open_batches": [], "today_spend_usd": 1.25}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -k "verify_counts or analyze_status" -v`
Expected: FAIL (data not in envelope / wrong shape).

- [ ] **Step 3: Migrate `_cmd_verify_counts`**

```python
def _cmd_verify_counts(args: argparse.Namespace) -> int:
    out = args._out
    repos = [r.full for r in config.load_repos(args.config)]
    con = _open_db(args.db)
    results = verify_mod.verify_counts(con, repos, tolerance=args.tolerance)
    rows = [
        {
            "repo": r["repo"],
            "mirror": r["mirror"],
            "github": r["github"],
            "diff": r["github"] - r["mirror"],
            "ok": r["ok"],
        }
        for r in results
    ]
    bad = [r for r in rows if not r["ok"]]
    lines = [
        f"{'OK ' if r['ok'] else 'MISMATCH'} {r['repo']}: mirror={r['mirror']} "
        f"github={r['github']} diff={r['diff']:+d}"
        for r in rows
    ]
    lines.append(f"{len(rows) - len(bad)}/{len(rows)} repos reconcile")
    data = {
        "reconciled": not bad,
        "tolerance": args.tolerance,
        "repos": rows,
    }
    return out.emit(data, "\n".join(lines), exit_code=1 if bad else 0)
```

- [ ] **Step 4: Migrate `_cmd_analyze_status`**

```python
def _cmd_analyze_status(args: argparse.Namespace) -> int:
    out = args._out
    con = _open_db(args.db)
    status = analyze_mod.analyze_status(con)
    lines = [
        f"open batches: {len(status['open_batches'])}; "
        f"today spend: ${status['today_spend_usd']:.4f}"
    ]
    lines += [f"  {b['batch_id']} [{b['stage']}] {b['status']}" for b in status["open_batches"]]
    return out.emit(status, "\n".join(lines))
```

- [ ] **Step 5: Run tests + full CLI suite**

Run: `uv run pytest tests/triage_verse/test_cli_json.py tests/triage_verse/test_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_json.py
git commit -m "feat(cli): --json for verify-counts and analyze-status (#42)"
```

---

### Task 4: Migrate `embed`, `analyze`, `tier1`, `tier2`

**Files:**
- Modify: `src/triage_verse/cli.py` (`_cmd_embed`, `_run_analyze` → return summary, `_cmd_analyze`, `_cmd_tier1`, `_cmd_tier2`)
- Test: `tests/triage_verse/test_cli_json.py` (append)

**Interfaces:**
- Consumes: `args._out`.
- Produces:
  - `_run_analyze(args) -> dict` now **returns** the analyze summary (so both `analyze` and `steady-state` can reuse it); it uses `args._out.log` for logging.
  - `embed` data `{embedded}`; `analyze` data `{classified, rechecked, pairs, halted_on_budget}`; `tier1` data `{sessions, proposals, halted_on_budget}`; `tier2` data `{repo, number, label, workflow_hint}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_cli_json.py`:

```python
from triage_verse import embed as embed_mod


def test_tier2_json(tmp_path, monkeypatch, capsys):
    from triage_verse import tier2, gh

    monkeypatch.setattr(tier2, "request_fix", lambda repo, number, *, run_gh: None)
    rc = cli.main(["tier2", "--json", "rstudio/shiny#7"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["data"]["repo"] == "rstudio/shiny"
    assert doc["data"]["number"] == 7
    assert doc["data"]["label"] == tier2.LABEL


def test_tier2_bad_ref_json_error(capsys):
    rc = cli.main(["tier2", "--json", "not-a-ref"])
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and "not-a-ref" in doc["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -k tier2 -v`
Expected: FAIL (`data`/envelope missing).

- [ ] **Step 3: Migrate `_run_analyze` to return the summary**

Change `_run_analyze` to return the summary and log via `args._out` (it currently prints). Replace its body's tail:

```python
def _run_analyze(args: argparse.Namespace) -> dict:
    """Shared analyze logic used by both `analyze` and `steady-state`; returns the summary."""
    out = args._out
    cfg = config.load_models_config(args.models_config)
    con = _open_db(args.db)
    embedder = embed_mod.FastEmbedEmbedder(cfg.embed_model)
    summary = analyze_mod.analyze(
        con,
        cfg,
        repo=args.repo,
        limit=args.limit,
        full=args.full,
        wait=args.wait,
        embedder=embedder,
        batch_client=llm.make_batch_client(cfg, log=out.log),
        rubric_path=".github/triage/issue-triage-rubric.md",
        labels_path=".github/triage/labels.yaml",
        proposals_dir=args.proposals_dir,
        log=out.log,
    )
    return summary
```

- [ ] **Step 4: Migrate `_cmd_analyze`, `_cmd_embed`, `_cmd_tier1`, `_cmd_tier2`**

```python
def _cmd_analyze(args: argparse.Namespace) -> int:
    summary = _run_analyze(args)
    human = (
        f"classified={summary['classified']} rechecked={summary['rechecked']} "
        f"pairs={summary['pairs']} halted_on_budget={summary['halted_on_budget']}"
    )
    return args._out.emit(summary, human)


def _cmd_embed(args: argparse.Namespace) -> int:
    out = args._out
    cfg = config.load_models_config(args.models_config)
    repos = [r.full for r in config.load_repos(args.config)]
    if args.repo:
        repos = [args.repo]
    con = _open_db(args.db)
    embedder = embed_mod.FastEmbedEmbedder(cfg.embed_model)
    total = sum(embed_mod.embed_repo(con, r, embedder, full=args.full) for r in repos)
    return out.emit({"embedded": total}, f"embedded {total} issues")


def _cmd_tier1(args: argparse.Namespace) -> int:
    out = args._out
    con = _open_db(args.db)
    repos = (
        [args.repo] if args.repo else [r.full for r in config.load_repos(args.config)]
    )
    cfg = config.load_models_config(args.models_config)
    from . import tier1

    res = tier1.run(
        con, repos, cfg=cfg, proposals_dir=args.proposals_dir, run_gh=gh.run_gh
    )
    human = (
        f"tier1: {res['sessions']} sessions, {res['proposals']} close proposals"
        f"{' (halted on budget)' if res['halted_on_budget'] else ''}"
    )
    return out.emit(res, human)


def _cmd_tier2(args: argparse.Namespace) -> int:
    out = args._out
    from . import executor, tier2

    ref = executor.parse_issue_ref(args.issue, default_repo="")
    if ref is None:
        return out.fail(f"cannot parse issue ref {args.issue!r}")
    tier2.request_fix(ref[0], ref[1], run_gh=gh.run_gh)
    workflow_hint = (
        f"gh workflow run tier2-fix.yml -f issue={args.issue} -f model={args.model}"
    )
    data = {
        "repo": ref[0],
        "number": ref[1],
        "label": tier2.LABEL,
        "workflow_hint": workflow_hint,
    }
    human = f"labeled {ref[0]}#{ref[1]} with {tier2.LABEL}\nkick off the fix: {workflow_hint}"
    return out.emit(data, human)
```

- [ ] **Step 5: Run tests + full CLI suite**

Run: `uv run pytest tests/triage_verse/test_cli_json.py tests/triage_verse/test_cli.py tests/triage_verse/test_cli_analyze.py -v`
Expected: PASS. If `test_cli_analyze.py` asserted on printed prose from `_run_analyze`, adjust it to construct/pass `args._out` — but it invokes via `cli.main`, so `args._out` is set by `main()` and prose still prints in human mode.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_json.py
git commit -m "feat(cli): --json for embed, analyze, tier1, tier2 (#42)"
```

---

### Task 5: Migrate `execute` and `undo` (domain exit code)

**Files:**
- Modify: `src/triage_verse/cli.py` (`_cmd_execute`, `_cmd_undo`)
- Test: `tests/triage_verse/test_cli_json.py` (append)

**Interfaces:**
- Consumes: `args._out`.
- Produces: both commands' data `{batch_id, counts}`; `exit_code=1` when `counts["error"]` is truthy.

- [ ] **Step 1: Write the failing test**

Append to `tests/triage_verse/test_cli_json.py`:

```python
def test_execute_json_error_count_is_ok_true_exit_1(tmp_path, monkeypatch, capsys):
    from triage_verse import executor as executor_mod

    monkeypatch.setattr(
        executor_mod,
        "execute",
        lambda con, **kw: {"batch_id": "b1", "counts": {"applied": 3, "error": 1}},
    )
    rc = cli.main(["execute", "--json", "--db", str(tmp_path / "m.sqlite")])
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True
    assert doc["exit_code"] == 1
    assert doc["data"] == {"batch_id": "b1", "counts": {"applied": 3, "error": 1}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -k execute_json -v`
Expected: FAIL.

- [ ] **Step 3: Migrate `_cmd_execute` and `_cmd_undo`**

In `_cmd_execute`, keep all the `_env_default` setup lines unchanged; replace the tail (`summary = executor_mod.execute(...)` onward):

```python
    out = args._out
    con = _open_db(args.db)
    summary = executor_mod.execute(
        con,
        decisions_dir=args.decisions_dir,
        proposals_dir=args.proposals_dir,
        results_dir=args.results_dir,
        labels_path=args.labels,
        templates_dir=args.templates,
        run_gh=gh.run_gh,
        apply=args.apply,
        auto=args.auto,
        autonomy_path=args.autonomy,
        repo=args.repo,
        limit=args.limit,
    )
    rc = 1 if summary["counts"]["error"] else 0
    return out.emit(summary, f"batch {summary['batch_id']}: {summary['counts']}", exit_code=rc)
```

In `_cmd_undo`, keep the `_env_default` setup lines; replace the tail (`summary = executor_mod.undo(...)` onward):

```python
    out = args._out
    con = _open_db(args.db)
    summary = executor_mod.undo(
        con,
        results_dir=args.results_dir,
        batch_id=args.batch,
        issue=args.issue,
        run_gh=gh.run_gh,
        apply=args.apply,
    )
    rc = 1 if summary["counts"]["error"] else 0
    return out.emit(summary, f"batch {summary['batch_id']}: {summary['counts']}", exit_code=rc)
```

- [ ] **Step 4: Run tests + full CLI suite**

Run: `uv run pytest tests/triage_verse/test_cli_json.py tests/triage_verse/test_cli_execute.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_json.py
git commit -m "feat(cli): --json for execute and undo (#42)"
```

---

### Task 6: Migrate `analytics export` (module returns payload)

**Files:**
- Modify: `src/triage_verse/analytics.py:92-118` (`export` returns the payload)
- Modify: `src/triage_verse/cli.py` (`_cmd_analytics_export`)
- Test: `tests/triage_verse/test_cli_json.py` (append); check `tests/triage_verse/test_analytics*.py` if present.

**Interfaces:**
- Produces: `analytics_mod.export(con, out_path) -> dict` (the payload it wrote). `analytics export` data = that payload.

- [ ] **Step 1: Write the failing test**

Append to `tests/triage_verse/test_cli_json.py`:

```python
def test_analytics_export_json_emits_payload(tmp_path, monkeypatch, capsys):
    from triage_verse import analytics as analytics_mod

    payload = {"generated_at": "2026-07-15T00:00:00Z", "totals": {}, "repos": {}}
    monkeypatch.setattr(analytics_mod, "export", lambda con, out_path: payload)
    rc = cli.main(
        ["analytics", "export", "--json", "--db", str(tmp_path / "m.sqlite"), "--out", str(tmp_path / "a.json")]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "analytics export"
    assert doc["data"] == payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -k analytics_export -v`
Expected: FAIL.

- [ ] **Step 3: Make `export` return the payload**

In `src/triage_verse/analytics.py`, change the signature and add a return. Signature line:

```python
def export(con: sqlite3.Connection, out_path: str | pathlib.Path) -> dict:
```

Append after `tmp.replace(out_path)` (the last line of the function):

```python
    return payload
```

- [ ] **Step 4: Migrate `_cmd_analytics_export`**

```python
def _cmd_analytics_export(args: argparse.Namespace) -> int:
    out = args._out
    con = _open_db(args.db)
    payload = analytics_mod.export(con, args.out)
    return out.emit(payload, f"wrote {args.out}")
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -k analytics_export -v && uv run pytest tests/triage_verse -k analytics -v`
Expected: PASS. (If an existing analytics test asserted `export` returns `None`, update it to accept the dict — it now returns the payload.)

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/analytics.py src/triage_verse/cli.py tests/triage_verse/test_cli_json.py
git commit -m "feat(cli): --json for analytics export; export returns payload (#42)"
```

---

### Task 7: Migrate `snapshot publish/bootstrap` and `state pull/push`

**Files:**
- Modify: `src/triage_verse/cli.py` (`_cmd_snapshot_publish`, `_cmd_snapshot_bootstrap`, `_cmd_state_pull`, `_cmd_state_push`)
- Test: `tests/triage_verse/test_cli_json.py` (append)

**Interfaces:**
- Consumes: `args._out`; `snapshot_mod.publish() -> str` (a tag), `snapshot_mod.bootstrap() -> None`, `state.pull()/push() -> dict`.
- Produces: `snapshot publish` data `{tag, latest_tag}`; `snapshot bootstrap` data `{db, tag}`; `state pull`/`push` data = the module result dict.

- [ ] **Step 1: Write the failing test**

Append to `tests/triage_verse/test_cli_json.py`:

```python
def test_snapshot_publish_json(tmp_path, monkeypatch, capsys):
    from triage_verse import snapshot as snapshot_mod

    monkeypatch.setattr(snapshot_mod, "publish", lambda db, *, dated: snapshot_mod.LATEST_TAG)
    rc = cli.main(["snapshot", "publish", "--json", "--db", str(tmp_path / "m.sqlite")])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "snapshot publish"
    assert doc["data"] == {"tag": snapshot_mod.LATEST_TAG, "latest_tag": snapshot_mod.LATEST_TAG}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -k snapshot_publish -v`
Expected: FAIL.

- [ ] **Step 3: Migrate the four handlers**

```python
def _cmd_snapshot_publish(args: argparse.Namespace) -> int:
    out = args._out
    tag = snapshot_mod.publish(args.db, dated=args.dated)
    if tag == snapshot_mod.LATEST_TAG:
        human = f"published snapshot to release {tag}"
    else:
        human = f"published snapshot to releases {tag} and {snapshot_mod.LATEST_TAG}"
    return out.emit({"tag": tag, "latest_tag": snapshot_mod.LATEST_TAG}, human)


def _cmd_snapshot_bootstrap(args: argparse.Namespace) -> int:
    out = args._out
    snapshot_mod.bootstrap(args.db, force=args.force)
    return out.emit(
        {"db": args.db, "tag": snapshot_mod.LATEST_TAG},
        f"bootstrapped {args.db} from {snapshot_mod.LATEST_TAG}",
    )


def _cmd_state_pull(args: argparse.Namespace) -> int:
    from . import state

    out = args._out
    work = os.environ.get("TRIAGE_VERSE_STATE_WORKDIR", ".data/triage-state")
    _ensure_state_clone(work, args.branch)
    res = state.pull(
        data_dir=args.data_dir, work_dir=work, run_git=_run_git, branch=args.branch
    )
    return out.emit(res, f"pulled: {res['files_updated']} files updated")


def _cmd_state_push(args: argparse.Namespace) -> int:
    from . import state

    out = args._out
    con = _open_db(args.db)
    repos = [r.full for r in config.load_repos(args.config)]
    work = os.environ.get("TRIAGE_VERSE_STATE_WORKDIR", ".data/triage-state")
    _ensure_state_clone(work, args.branch)
    res = state.push(
        con,
        repos,
        data_dir=args.data_dir,
        work_dir=work,
        run_git=_run_git,
        branch=args.branch,
        now=_state_now(),
    )
    human = f"push: {'committed' if res['pushed'] else 'no changes'} ({res['records']} records)"
    return out.emit(res, human)
```

- [ ] **Step 4: Run tests + full CLI suite**

Run: `uv run pytest tests/triage_verse/test_cli_json.py tests/triage_verse/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_json.py
git commit -m "feat(cli): --json for snapshot and state commands (#42)"
```

---

### Task 8: Migrate `autonomy status` and `steady-state`

**Files:**
- Modify: `src/triage_verse/cli.py` (`_cmd_autonomy_status`, `_cmd_steady_state`)
- Test: `tests/triage_verse/test_cli_json.py` (append)

**Interfaces:**
- Consumes: `args._out`; `autonomy.evaluate() -> dict`; `steady_state.run(stages) -> {completed, failed, error?}`.
- Produces: `autonomy status` data `{categories, wrote}`; `steady-state` dry-run data `{stages, dry_run:true}`, real-run data = the `steady_state.run` result (with `exit_code=1` when `failed`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_cli_json.py`:

```python
def test_steady_state_dry_run_json(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    rc = cli.main(
        ["steady-state", "--json", "--dry-run", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "steady-state"
    assert doc["data"]["dry_run"] is True
    assert "state-pull" in doc["data"]["stages"]


def test_autonomy_status_json_empty(tmp_path, monkeypatch, capsys):
    from triage_verse import autonomy, review_queue

    monkeypatch.setattr(review_queue, "iter_jsonl_records", lambda d: [])
    monkeypatch.setattr(autonomy, "evaluate", lambda decisions, results, cfg: {})
    rc = cli.main(
        ["autonomy", "status", "--json", "--decisions-dir", str(tmp_path), "--results-dir", str(tmp_path)]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "autonomy status"
    assert doc["data"] == {"categories": {}, "wrote": None}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_cli_json.py -k "steady_state_dry_run or autonomy_status_json" -v`
Expected: FAIL.

- [ ] **Step 3: Migrate `_cmd_autonomy_status`**

```python
def _cmd_autonomy_status(args: argparse.Namespace) -> int:
    from . import autonomy, review_queue
    import yaml

    out = args._out
    cfg = config.load_models_config(args.models_config).autonomy
    decisions = review_queue.iter_jsonl_records(args.decisions_dir)
    results = review_queue.iter_jsonl_records(args.results_dir)
    ev = autonomy.evaluate(decisions, results, cfg)
    lines = [
        f"{action}: reviewed={e['reviewed']} precision={e['precision']:.3f}"
        f" audit_fail={e['audit_failures']} -> {'PROMOTE' if e['promote'] else 'hold'}"
        for action, e in sorted(ev.items())
    ]
    if not ev:
        lines.append("no eligible categories with reviewed decisions yet")
    wrote = None
    if args.write:
        doc = autonomy.render_config(ev, cfg, today=_state_now()[:10])
        pathlib.Path(args.out).write_text(
            yaml.safe_dump(doc, sort_keys=True), encoding="utf-8"
        )
        wrote = args.out
        lines.append(f"wrote {args.out}")
    return out.emit({"categories": ev, "wrote": wrote}, "\n".join(lines))
```

- [ ] **Step 4: Migrate `_cmd_steady_state`**

Keep the whole stage-setup body unchanged (the `con`, `repos`, `work`, the `_pull`/`_sync`/... closures using `print` — leave those as `print` for now, they are internal to steady-state's own stage logging). Replace only the tail (from `stages = [...]` onward):

```python
    out = args._out
    stages = [
        ("state-pull", _pull),
        ("sync", _sync),
        ("embed-analyze", _analyze),
        ("tier1", _tier1),
        ("state-push", _push),
        ("snapshot", _snapshot),
    ]
    if args.dry_run:
        names = [name for name, _ in stages]
        human = "\n".join(f"would run: {name}" for name in names)
        return out.emit({"stages": names, "dry_run": True}, human)
    res = steady_state.run(stages)
    human = f"steady-state: completed={res['completed']} failed={res['failed']}"
    return out.emit(res, human, exit_code=1 if res["failed"] else 0)
```

Note: `_analyze` inside `_cmd_steady_state` calls `_run_analyze(args)`, which now returns a summary and logs via `args._out.log` — no change needed there; its return value is ignored by the closure.

- [ ] **Step 5: Run the full test suite**

Run: `make py-check`
Expected: PASS (ruff format + lint clean, pyright clean, all pytest green).

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_json.py
git commit -m "feat(cli): --json for autonomy status and steady-state (#42)"
```

---

### Task 9: Document `--json` and final gate

**Files:**
- Modify: `README.md` (CLI section — document the global `--json` flag and the envelope)
- Modify: `CLAUDE.md` only if it enumerates CLI flags (check; likely no change)

**Interfaces:** none (docs).

- [ ] **Step 1: Add README documentation**

Find the CLI section in `README.md` (search for `uv run triage-verse`). Add a short subsection:

```markdown
### Machine-readable output (`--json`)

Every command accepts a global `--json` flag, in either position
(`triage-verse --json sync` or `triage-verse sync --json`). With it, the command
prints a single JSON envelope on stdout and sends all human/progress logging to
stderr:

    {"command": "sync", "ok": true, "exit_code": 0,
     "data": {"repos": 2, "issues": 4, "prs": 2, "comments": 6}}

- `ok` is `true` when the command ran to completion. A command that ran fine but
  reports a negative result (e.g. `verify-counts` found a mismatch) is still
  `ok: true`, with a non-zero `exit_code` and the details in `data`.
- Bad input or an unexpected error gives `{"ok": false, "error": "..."}` with a
  non-zero `exit_code`.
- `exit_code` mirrors the process exit code, so shell callers can branch on
  either the field or `$?`.
```

- [ ] **Step 2: Verify docs render / no broken references**

Run: `make check`
Expected: PASS (validate-yaml, compile-scripts, py-check, js-check all green).

- [ ] **Step 3: Manual smoke test**

Run:
```bash
uv run triage-verse verify-counts --json --db .data/mirror.sqlite | python -m json.tool
uv run triage-verse --json analyze-status --db .data/mirror.sqlite
```
Expected: each prints a single well-formed JSON envelope on stdout; any progress logging appears on stderr (not inside the piped JSON).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document CLI --json output mode (#42)"
```

---

## Self-Review

**Spec coverage:**
- Global `--json`, either position → Task 2 (parser wiring, both-position tests). ✓
- All commands → Tasks 2–8 cover every subcommand in the `data`-payload table. ✓
- Envelope `{command, ok, exit_code, data|error}` → Task 1 (`Output`), constraints. ✓
- Logs to stderr → Task 1 `log()`, Task 2 stderr test. ✓
- `ok` semantics (domain-negative = ok:true, exit 1) → Tasks 3 (verify-counts) & 5 (execute). ✓
- Structured errors + preserved exit codes → Task 2 (bad input, exception wrapper). ✓
- `analytics export` stdout support → Task 6 (module returns payload). ✓
- Human mode unchanged → every task re-runs existing `test_cli*.py`; Task 2 covers the one behavior nuance (`fail` prints to stderr). ✓
- Docs → Task 9. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows the test body. ✓

**Type consistency:** `Output.emit(data, human, exit_code=0)` / `Output.fail(message, exit_code=1)` / `Output.log(msg)` used identically across Tasks 2–8. `args._out` set in `main()` (Task 2) and read by all handlers. `_run_analyze(args) -> dict` (Task 4) consumed by `_cmd_analyze` and the steady-state closure. `analytics_mod.export(...) -> dict` (Task 6) consumed by `_cmd_analytics_export`. Command-name strings match the `cmdname=` set_defaults in Task 2. ✓
