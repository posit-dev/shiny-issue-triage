# Plan 4: Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `triage-verse execute` applies human-approved decisions to GitHub (dry-run default, freshness-checked, allowlisted, templated comments) and `triage-verse undo` reverses an executed batch.

**Architecture:** New `executor.py` (selection → freshness → allowlisted mutation dispatch → results log → mirror update, plus undo) and `templates.py` (load/validate/render `config/templates/*.md`), wired into the existing `cli.py`. All GitHub access goes through a `run_gh`-shaped callable (`gh.run_gh` in production, a stateful fake in tests). `review_queue.load_undecided` learns to resurface stale-bounced proposals.

**Tech Stack:** Python 3.14, stdlib only (argparse/json/sqlite3/pathlib/uuid/datetime), `gh` CLI at runtime, pytest.

**Spec:** `docs/superpowers/specs/2026-07-13-plan-4-executor-design.md` — read it before starting any task.

## Global Constraints

- Dry-run is the default for both `execute` and `undo`; mutations require `--apply`.
- Only allowlisted mutations, ever: label add/remove (labels validated against `.github/triage/labels.yaml`), templated comment, close (`completed` / `not planned`), close-as-duplicate, reopen (undo only).
- Proposal `rationale` (model output) is never posted to GitHub.
- Comment bodies come only from `config/templates/*.md`; the complete placeholder set is `{canonical_url}`.
- Results log: `.data/results/YYYY/Www.jsonl` via `jsonl_log.append_weekly` (already gitignored under `.data/`).
- Mutation pacing: 1 second between mutating `gh` calls, injectable for tests.
- Errors on one decision never abort the batch; CLI exits non-zero if any record ended `error`.
- Run `uv run pytest tests/ -q` for tests; `make check` before the final commit (runs ruff, pyright, pytest, yaml validation).
- Code style: `from __future__ import annotations`, module docstring first line, type hints on public functions (pyright runs in CI), stdlib `logging` not print inside library code (CLI handlers may print).

---

## Existing interfaces the tasks build on (read-only reference)

- `gh.run_gh(args: list[str], *, input: str | None = None, retries: int = 5, sleep=time.sleep) -> str` — runs `["gh", *args]`, returns stdout, raises `gh.GhError` on failure.
- `jsonl_log.append_weekly(records: list[dict], base_dir, *, today: str | None = None) -> pathlib.Path` — appends to `<base>/<ISO year>/W<ISO week>.jsonl`.
- `review_queue.iter_jsonl_records(base_dir) -> list[dict]` — reads every `**/*.jsonl` record under a dir (skips malformed lines).
- `prompts.allowed_labels(labels_path) -> set[str]` — the `allowed_safe_output_labels` set from `.github/triage/labels.yaml` (contains classification labels and `Priority: *` labels).
- `db.connect(path)`, `db.get_issue(con, repo, number) -> sqlite3.Row | None` — issues table columns include `labels_json` (JSON list of names), `state` (`"OPEN"`/`"CLOSED"`), `state_reason` (uppercase GraphQL enum or None).
- Decision record (from `decisions.record`): `{id, proposal_id, repo, issue, action, params, verdict, confidence, decided_at}`; verdict ∈ `approved|rejected|skipped|edited`; for `edited`, `params` is the reviewer's edit and `proposed_params` the original.
- Proposal record (from `proposals.build`): `{id, repo, issue, issue_updated_at, run_id, model, confidence, evidence, action, params, rationale}`. Params by action: `add-label` → `{"label": str}`; `set-priority` → `{"priority": "Critical|High|Medium|Low"}`; `close` → `{"reason": "duplicate|stale|not-planned|fixed|answered"}`; `close-duplicate` → `{"canonical": str | None, "cross_repo_option": str | None}`.
- REST issue fetch (`gh api repos/{repo}/issues/{n}`) returns JSON with `updated_at`, `node_id`, `state` (`"open"`/`"closed"`), `state_reason` (lowercase or null), `labels: [{"name": …}, …]`.

---

### Task 1: Comment templates (`templates.py` + `config/templates/`)

**Files:**
- Create: `config/templates/close-completed.md`
- Create: `config/templates/close-not-planned.md`
- Create: `config/templates/close-duplicate.md`
- Create: `config/templates/close-duplicate-cross-repo.md`
- Create: `src/triage_verse/templates.py`
- Test: `tests/triage_verse/test_templates.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `templates.load(base_dir: str | pathlib.Path) -> dict[str, str]` (raises `templates.TemplateError`), `templates.render(loaded: dict[str, str], name: str, **values: str) -> str`, constants `templates.TEMPLATE_NAMES`, `templates.ALLOWED_PLACEHOLDERS`, `templates.DEFAULT_DIR = "config/templates"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_templates.py
"""Tests for executor comment templates."""

import pathlib

import pytest

from triage_verse import templates

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REAL_DIR = REPO_ROOT / "config" / "templates"


def test_load_real_templates_and_render_duplicate():
    loaded = templates.load(REAL_DIR)
    assert set(loaded) == set(templates.TEMPLATE_NAMES)
    body = templates.render(
        loaded, "close-duplicate", canonical_url="https://github.com/o/r/issues/1"
    )
    assert "https://github.com/o/r/issues/1" in body
    assert body.endswith("\n")
    # Every close template invites reopening (community-trust requirement).
    for name in templates.TEMPLATE_NAMES:
        assert "reopen" in loaded[name]


def test_missing_template_file_raises(tmp_path):
    with pytest.raises(templates.TemplateError, match="missing template"):
        templates.load(tmp_path)


def test_unknown_placeholder_raises(tmp_path):
    for name in templates.TEMPLATE_NAMES:
        (tmp_path / f"{name}.md").write_text("ok", encoding="utf-8")
    (tmp_path / "close-completed.md").write_text("hi {rationale}", encoding="utf-8")
    with pytest.raises(templates.TemplateError, match="unknown placeholder"):
        templates.load(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_templates.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_verse.templates'` (import error counts as failure for all three).

- [ ] **Step 3: Write the template files (exact wording from the spec)**

`config/templates/close-completed.md`:

```markdown
This issue appears to have been resolved, so we're closing it as completed as part of a maintainer-reviewed triage of the backlog.

If you're still seeing this with the latest release, please leave a comment and we'll gladly reopen it.
```

`config/templates/close-not-planned.md`:

```markdown
As part of a maintainer-reviewed triage of the backlog, we've decided not to move forward with this issue, so we're closing it as not planned.

If you think this deserves another look, please leave a comment and we'll gladly reopen it.
```

`config/templates/close-duplicate.md`:

```markdown
This looks like a duplicate of {canonical_url}, so we're closing this one to consolidate the discussion there. This close was reviewed and approved by a maintainer as part of a triage of the backlog.

If your report differs from that issue, please leave a comment and we'll gladly reopen it.
```

`config/templates/close-duplicate-cross-repo.md`:

```markdown
This looks like a duplicate of {canonical_url}, which lives in a different repository — we're closing this one to consolidate the discussion there, and we'd encourage you to follow that issue for updates. This close was reviewed and approved by a maintainer as part of a triage of the backlog.

If your report differs from that issue, please leave a comment and we'll gladly reopen it.
```

- [ ] **Step 4: Write `src/triage_verse/templates.py`**

```python
"""Load, validate, and render executor comment templates."""

from __future__ import annotations

import pathlib
import string

TEMPLATE_NAMES = (
    "close-completed",
    "close-not-planned",
    "close-duplicate",
    "close-duplicate-cross-repo",
)
ALLOWED_PLACEHOLDERS = frozenset({"canonical_url"})
DEFAULT_DIR = "config/templates"


class TemplateError(ValueError):
    pass


def load(base_dir: str | pathlib.Path) -> dict[str, str]:
    base = pathlib.Path(base_dir)
    loaded: dict[str, str] = {}
    for name in TEMPLATE_NAMES:
        path = base / f"{name}.md"
        if not path.exists():
            raise TemplateError(f"missing template: {path}")
        text = path.read_text(encoding="utf-8")
        for field in _fields(text):
            if field not in ALLOWED_PLACEHOLDERS:
                raise TemplateError(f"{path}: unknown placeholder {{{field}}}")
        loaded[name] = text
    return loaded


def _fields(text: str) -> list[str]:
    return [f for _, f, _, _ in string.Formatter().parse(text) if f]


def render(loaded: dict[str, str], name: str, **values: str) -> str:
    return loaded[name].format(**values).strip() + "\n"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_templates.py -q`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add config/templates src/triage_verse/templates.py tests/triage_verse/test_templates.py
git commit -m "feat(executor): approved comment templates with placeholder validation"
```

---

### Task 2: Decision selection (`executor.select_executable` + proposal join)

**Files:**
- Create: `src/triage_verse/executor.py`
- Test: `tests/triage_verse/test_executor_select.py`

**Interfaces:**
- Consumes: `review_queue.iter_jsonl_records`.
- Produces (used by Tasks 4–6):
  - `executor.FINAL_STATUSES = frozenset({"applied", "stale-needs-rereview", "error"})`
  - `executor.select_executable(decisions: list[dict], results: list[dict]) -> list[dict]` — latest approved/edited decision per `proposal_id`, minus decisions already finalized in results; sorted by `decided_at` then `id` for deterministic order.
  - `executor.index_proposals(proposals: list[dict]) -> dict[str, dict]` — proposal id → record.
  - `executor._now() -> str` — UTC `%Y-%m-%dT%H:%M:%SZ`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_executor_select.py
"""Tests for executor decision selection."""

from triage_verse import executor


def _decision(pid, verdict, decided_at, did=None):
    return {
        "id": did or f"d-{pid}-{decided_at}",
        "proposal_id": pid,
        "repo": "o/r",
        "issue": 1,
        "action": "add-label",
        "params": {"label": "regression"},
        "verdict": verdict,
        "decided_at": decided_at,
    }


def test_keeps_only_approving_verdicts():
    decisions = [
        _decision("p1", "approved", "2026-07-13T00:00:00Z"),
        _decision("p2", "rejected", "2026-07-13T00:00:00Z"),
        _decision("p3", "skipped", "2026-07-13T00:00:00Z"),
        _decision("p4", "edited", "2026-07-13T00:00:00Z"),
    ]
    picked = executor.select_executable(decisions, [])
    assert {d["proposal_id"] for d in picked} == {"p1", "p4"}


def test_latest_decision_per_proposal_wins():
    decisions = [
        _decision("p1", "approved", "2026-07-12T00:00:00Z"),
        _decision("p1", "rejected", "2026-07-13T00:00:00Z"),
        _decision("p2", "rejected", "2026-07-12T00:00:00Z"),
        _decision("p2", "approved", "2026-07-13T00:00:00Z"),
    ]
    picked = executor.select_executable(decisions, [])
    assert {d["proposal_id"] for d in picked} == {"p2"}


def test_finalized_results_block_reexecution_but_dry_run_does_not():
    d1 = _decision("p1", "approved", "2026-07-13T00:00:00Z", did="d1")
    d2 = _decision("p2", "approved", "2026-07-13T00:00:00Z", did="d2")
    d3 = _decision("p3", "approved", "2026-07-13T00:00:00Z", did="d3")
    d4 = _decision("p4", "approved", "2026-07-13T00:00:00Z", did="d4")
    results = [
        {"decision_id": "d1", "status": "applied"},
        {"decision_id": "d2", "status": "dry-run"},
        {"decision_id": "d3", "status": "stale-needs-rereview"},
        {"decision_id": "d4", "status": "error"},
    ]
    picked = executor.select_executable([d1, d2, d3, d4], results)
    assert [d["id"] for d in picked] == ["d2"]


def test_index_proposals():
    p = {"id": "p1", "action": "close"}
    assert executor.index_proposals([p]) == {"p1": p}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_executor_select.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_verse.executor'`

- [ ] **Step 3: Write the first slice of `src/triage_verse/executor.py`**

```python
"""Apply approved review decisions to GitHub, with batch undo."""

from __future__ import annotations

from datetime import datetime, timezone

FINAL_STATUSES = frozenset({"applied", "stale-needs-rereview", "error"})
EXECUTABLE_VERDICTS = frozenset({"approved", "edited"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_executable(decisions: list[dict], results: list[dict]) -> list[dict]:
    """Latest approved/edited decision per proposal, minus already-finalized ones."""
    latest: dict[str, dict] = {}
    for d in decisions:
        pid = d.get("proposal_id")
        if pid is None:
            continue
        cur = latest.get(pid)
        if cur is None or d.get("decided_at", "") > cur.get("decided_at", ""):
            latest[pid] = d
    finalized = {
        r["decision_id"]
        for r in results
        if r.get("status") in FINAL_STATUSES and "decision_id" in r
    }
    picked = [
        d
        for d in latest.values()
        if d.get("verdict") in EXECUTABLE_VERDICTS and d["id"] not in finalized
    ]
    return sorted(picked, key=lambda d: (d.get("decided_at", ""), d["id"]))


def index_proposals(proposals: list[dict]) -> dict[str, dict]:
    return {p["id"]: p for p in proposals if "id" in p}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_executor_select.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/executor.py tests/triage_verse/test_executor_select.py
git commit -m "feat(executor): select latest executable decisions against results ledger"
```

---

### Task 3: Mutation planning + allowlist validation (`executor.plan_decision`)

**Files:**
- Modify: `src/triage_verse/executor.py` (append to Task 2's file)
- Test: `tests/triage_verse/test_executor_plan.py`

**Interfaces:**
- Consumes: `templates.render` / a loaded templates dict (Task 1); `prompts.allowed_labels` output (a `set[str]`).
- Produces (used by Task 4):
  - `executor.parse_issue_ref(text: str, default_repo: str) -> tuple[str, int] | None` — accepts `owner/name#N`, `https://github.com/owner/name/issues/N`, `#N`, or `N` (last two resolve against `default_repo`).
  - `executor.plan_decision(decision: dict, issue: dict, *, allowed: set[str], tmpl: dict[str, str]) -> tuple[list[dict], str | None]` — returns `(mutations, None)` or `([], error_message)`. `issue` is the parsed REST fetch. Mutation dicts (complete vocabulary):
    - `{"kind": "add-label", "label": str}`
    - `{"kind": "remove-label", "label": str}`
    - `{"kind": "comment", "body": str}`
    - `{"kind": "close", "reason": "completed" | "not planned"}`
    - `{"kind": "close-duplicate", "canonical": [repo, number]}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_executor_plan.py
"""Tests for executor mutation planning and allowlist validation."""

import pathlib

import pytest

from triage_verse import executor, templates

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TMPL = templates.load(REPO_ROOT / "config" / "templates")
ALLOWED = {
    "regression",
    "duplicate",
    "Priority: Critical",
    "Priority: High",
    "Priority: Low",
}


def _decision(action, params, repo="o/r", issue=7):
    return {
        "id": "d1",
        "proposal_id": "p1",
        "repo": repo,
        "issue": issue,
        "action": action,
        "params": params,
        "verdict": "approved",
        "decided_at": "2026-07-13T00:00:00Z",
    }


def _issue(labels=(), state="open"):
    return {
        "updated_at": "2026-07-01T00:00:00Z",
        "node_id": "NID",
        "state": state,
        "state_reason": None,
        "labels": [{"name": name} for name in labels],
    }


@pytest.mark.parametrize(
    "text,expected",
    [
        ("o/r#12", ("o/r", 12)),
        ("other/repo#3", ("other/repo", 3)),
        ("https://github.com/o/r/issues/12", ("o/r", 12)),
        ("#12", ("o/r", 12)),
        ("12", ("o/r", 12)),
        ("nonsense", None),
    ],
)
def test_parse_issue_ref(text, expected):
    assert executor.parse_issue_ref(text, "o/r") == expected


def test_add_label_plans_single_mutation():
    muts, err = executor.plan_decision(
        _decision("add-label", {"label": "regression"}),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert muts == [{"kind": "add-label", "label": "regression"}]


def test_add_label_rejects_unlisted_label():
    muts, err = executor.plan_decision(
        _decision("add-label", {"label": "evil"}), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and err is not None and "evil" in err


def test_set_priority_swaps_existing_priority_labels():
    muts, err = executor.plan_decision(
        _decision("set-priority", {"priority": "High"}),
        _issue(labels=["Priority: Low", "bug"]),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert muts == [
        {"kind": "remove-label", "label": "Priority: Low"},
        {"kind": "add-label", "label": "Priority: High"},
    ]


def test_set_priority_rejects_unknown_value():
    muts, err = executor.plan_decision(
        _decision("set-priority", {"priority": "Urgent"}),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert muts == [] and err is not None


@pytest.mark.parametrize(
    "reason,gh_reason,template_word",
    [
        ("fixed", "completed", "resolved"),
        ("answered", "completed", "resolved"),
        ("stale", "not planned", "not planned"),
        ("not-planned", "not planned", "not planned"),
    ],
)
def test_close_maps_reason_and_comments(reason, gh_reason, template_word):
    muts, err = executor.plan_decision(
        _decision("close", {"reason": reason}), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert err is None
    assert muts[0]["kind"] == "comment" and template_word in muts[0]["body"]
    assert muts[1] == {"kind": "close", "reason": gh_reason}


def test_close_reason_duplicate_is_an_error():
    muts, err = executor.plan_decision(
        _decision("close", {"reason": "duplicate"}), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and "close-duplicate" in err


def test_close_duplicate_same_repo():
    muts, err = executor.plan_decision(
        _decision("close-duplicate", {"canonical": "o/r#3", "cross_repo_option": None}),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert muts[0]["kind"] == "comment"
    assert "https://github.com/o/r/issues/3" in muts[0]["body"]
    assert muts[1] == {"kind": "close-duplicate", "canonical": ["o/r", 3]}


def test_close_duplicate_cross_repo_falls_back_to_not_planned():
    muts, err = executor.plan_decision(
        _decision(
            "close-duplicate",
            {"canonical": "other/repo#3", "cross_repo_option": "close-and-link"},
        ),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert "different repository" in muts[0]["body"]
    assert muts[1] == {"kind": "close", "reason": "not planned"}


@pytest.mark.parametrize(
    "params",
    [
        {"canonical": None, "cross_repo_option": None},
        {"canonical": "nonsense", "cross_repo_option": None},
        {"canonical": "o/r#7", "cross_repo_option": None},  # canonical == self
    ],
)
def test_close_duplicate_bad_canonical_is_an_error(params):
    muts, err = executor.plan_decision(
        _decision("close-duplicate", params), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and err is not None


def test_unknown_action_is_an_error():
    muts, err = executor.plan_decision(
        _decision("transfer", {}), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and "transfer" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_executor_plan.py -q`
Expected: FAIL — `AttributeError: module 'triage_verse.executor' has no attribute 'parse_issue_ref'` (and similar).

- [ ] **Step 3: Append the planning slice to `src/triage_verse/executor.py`**

Add `import re` and `from . import templates as templates_mod` to the imports, then:

```python
PRIORITY_VALUES = ("Critical", "High", "Medium", "Low")
CLOSE_REASON_MAP = {
    "fixed": ("completed", "close-completed"),
    "answered": ("completed", "close-completed"),
    "stale": ("not planned", "close-not-planned"),
    "not-planned": ("not planned", "close-not-planned"),
}

_REF_FULL = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_REF_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)$")
_REF_LOCAL = re.compile(r"^#?(\d+)$")


def parse_issue_ref(text: str, default_repo: str) -> tuple[str, int] | None:
    text = text.strip()
    for pattern in (_REF_FULL, _REF_URL):
        m = pattern.match(text)
        if m:
            return m.group(1), int(m.group(2))
    m = _REF_LOCAL.match(text)
    if m:
        return default_repo, int(m.group(1))
    return None


def _issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def plan_decision(
    decision: dict, issue: dict, *, allowed: set[str], tmpl: dict[str, str]
) -> tuple[list[dict], str | None]:
    """Turn one decision into allowlisted mutations, or an error message."""
    action = decision["action"]
    params = decision.get("params") or {}

    if action == "add-label":
        label = params.get("label")
        if label not in allowed:
            return [], f"label not in allowlist: {label!r}"
        return [{"kind": "add-label", "label": label}], None

    if action == "set-priority":
        priority = params.get("priority")
        if priority not in PRIORITY_VALUES:
            return [], f"unknown priority: {priority!r}"
        label = f"Priority: {priority}"
        if label not in allowed:
            return [], f"label not in allowlist: {label!r}"
        current = [entry["name"] for entry in issue.get("labels", [])]
        muts: list[dict] = [
            {"kind": "remove-label", "label": name}
            for name in current
            if name.startswith("Priority: ") and name != label
        ]
        muts.append({"kind": "add-label", "label": label})
        return muts, None

    if action == "close":
        reason = params.get("reason")
        if reason == "duplicate":
            return [], "close reason 'duplicate' must arrive as a close-duplicate proposal"
        if reason not in CLOSE_REASON_MAP:
            return [], f"unknown close reason: {reason!r}"
        gh_reason, template_name = CLOSE_REASON_MAP[reason]
        body = templates_mod.render(tmpl, template_name)
        return [
            {"kind": "comment", "body": body},
            {"kind": "close", "reason": gh_reason},
        ], None

    if action == "close-duplicate":
        canonical = params.get("canonical")
        if not canonical:
            return [], "close-duplicate requires a canonical target"
        ref = parse_issue_ref(str(canonical), decision["repo"])
        if ref is None:
            return [], f"cannot parse canonical issue ref: {canonical!r}"
        if ref == (decision["repo"], decision["issue"]):
            return [], "canonical target is the issue itself"
        url = _issue_url(*ref)
        if ref[0] == decision["repo"]:
            body = templates_mod.render(tmpl, "close-duplicate", canonical_url=url)
            return [
                {"kind": "comment", "body": body},
                {"kind": "close-duplicate", "canonical": [ref[0], ref[1]]},
            ], None
        body = templates_mod.render(
            tmpl, "close-duplicate-cross-repo", canonical_url=url
        )
        return [
            {"kind": "comment", "body": body},
            {"kind": "close", "reason": "not planned"},
        ], None

    return [], f"action not allowlisted: {action!r}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_executor_plan.py tests/triage_verse/test_executor_select.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/executor.py tests/triage_verse/test_executor_plan.py
git commit -m "feat(executor): allowlisted mutation planning per decision"
```

---

### Task 4: `executor.execute` — freshness, dry-run, apply, results log, mirror update

**Files:**
- Modify: `src/triage_verse/executor.py`
- Create: `tests/triage_verse/fake_gh.py` (shared stateful fake; plain module, not a fixture file)
- Test: `tests/triage_verse/test_executor_execute.py`

**Interfaces:**
- Consumes: Tasks 1–3; `review_queue.iter_jsonl_records`; `jsonl_log.append_weekly`; `db` issues table.
- Produces (used by Tasks 5–6):
  - `executor.execute(con, *, decisions_dir, proposals_dir, results_dir, run_gh, apply=False, repo=None, limit=None, labels_path=".github/triage/labels.yaml", templates_dir=templates_mod.DEFAULT_DIR, pace=time.sleep, log=print) -> dict` returning `{"batch_id": str, "counts": {"applied": int, "dry-run": int, "stale-needs-rereview": int, "error": int}}`.
  - Result records as specified in the spec (`id`, `batch_id`, `decision_id`, `proposal_id`, `repo`, `issue`, `action`, `params`, `status`, `error?`, `prior`, `comment_id?`, `executed_at`).
  - `tests/triage_verse/fake_gh.FakeGh` — see Step 3.

- [ ] **Step 1: Write the stateful fake `gh`**

```python
# tests/triage_verse/fake_gh.py
"""Stateful in-memory fake for gh.run_gh, covering the executor's call surface."""

from __future__ import annotations

import json
import re


class FakeGh:
    """Callable standing in for gh.run_gh. Issues keyed by (repo, number)."""

    def __init__(self, issues: dict[tuple[str, int], dict]):
        # Each issue dict: labels (list[str]), state ("open"/"closed"),
        # state_reason (str|None), updated_at (str), node_id (str).
        self.issues = {k: dict(v) for k, v in issues.items()}
        self.comments: dict[int, dict] = {}  # comment id -> {repo, number, body}
        self._next_comment_id = 1000
        self.mutating_calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs) -> str:
        if args[0] == "api":
            return self._api(args)
        if args[0] == "issue":
            return self._issue_cmd(args)
        raise AssertionError(f"unexpected gh args: {args}")

    # -- helpers ---------------------------------------------------------

    def _find(self, repo: str, number: int) -> dict:
        return self.issues[(repo, number)]

    def _api(self, args: list[str]) -> str:
        if args[1] == "graphql":
            return self._graphql(args)
        if "-X" in args and "DELETE" in args:
            path = args[-1]
            m = re.match(r"repos/([\w.-]+/[\w.-]+)/issues/comments/(\d+)$", path)
            assert m, path
            self.mutating_calls.append(args)
            del self.comments[int(m.group(2))]
            return ""
        path = args[1]
        m = re.match(r"repos/([\w.-]+/[\w.-]+)/issues/(\d+)/comments$", path)
        if m:  # POST comment: gh api repos/.../comments -f body=...
            self.mutating_calls.append(args)
            body = next(
                a[len("body=") :] for a in args if a.startswith("body=")
            )
            cid = self._next_comment_id
            self._next_comment_id += 1
            self.comments[cid] = {
                "repo": m.group(1),
                "number": int(m.group(2)),
                "body": body,
            }
            issue = self._find(m.group(1), int(m.group(2)))
            issue["updated_at"] = issue["updated_at"] + "+c"
            return json.dumps({"id": cid})
        m = re.match(r"repos/([\w.-]+/[\w.-]+)/issues/(\d+)$", path)
        assert m, path
        issue = self._find(m.group(1), int(m.group(2)))
        return json.dumps(
            {
                "updated_at": issue["updated_at"],
                "node_id": issue["node_id"],
                "state": issue["state"],
                "state_reason": issue["state_reason"],
                "labels": [{"name": name} for name in issue["labels"]],
            }
        )

    def _graphql(self, args: list[str]) -> str:
        # closeIssue(stateReason: DUPLICATE, duplicateIssueId: ...)
        self.mutating_calls.append(args)
        fields = dict(
            a.split("=", 1) for a in args if "=" in a and not a.startswith("query=")
        )
        target = self._by_node_id(fields["issue"])
        assert self._by_node_id(fields["dup"]) is not None
        target["state"] = "closed"
        target["state_reason"] = "duplicate"
        return json.dumps({"data": {"closeIssue": {"issue": {"id": fields["issue"]}}}})

    def _by_node_id(self, node_id: str) -> dict:
        for issue in self.issues.values():
            if issue["node_id"] == node_id:
                return issue
        raise AssertionError(f"unknown node id {node_id}")

    def _issue_cmd(self, args: list[str]) -> str:
        self.mutating_calls.append(args)
        number = int(args[2])
        repo = args[args.index("--repo") + 1]
        issue = self._find(repo, number)
        if args[1] == "edit":
            for flag, value in zip(args, args[1:]):
                if flag == "--add-label" and value not in issue["labels"]:
                    issue["labels"] = [*issue["labels"], value]
                if flag == "--remove-label" and value in issue["labels"]:
                    issue["labels"] = [x for x in issue["labels"] if x != value]
        elif args[1] == "close":
            issue["state"] = "closed"
            reason = args[args.index("--reason") + 1]
            issue["state_reason"] = "completed" if reason == "completed" else "not_planned"
        elif args[1] == "reopen":
            issue["state"] = "open"
            issue["state_reason"] = "reopened"
        else:
            raise AssertionError(f"unexpected issue subcommand: {args}")
        issue["updated_at"] = issue["updated_at"] + "+m"
        return ""
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/triage_verse/test_executor_execute.py
"""End-to-end tests for executor.execute against a stateful fake gh."""

import json

from triage_verse import db, decisions, executor, jsonl_log, proposals, review_queue

from .fake_gh import FakeGh

LABELS_PATH = ".github/triage/labels.yaml"
UPDATED = "2026-07-01T00:00:00Z"


def _proposal(pid, action, params, issue=1, confidence=0.95):
    return {
        "id": pid,
        "repo": "o/r",
        "issue": issue,
        "issue_updated_at": UPDATED,
        "run_id": "run1",
        "model": "m",
        "confidence": confidence,
        "evidence": [],
        "action": action,
        "params": params,
        "rationale": "model text that must never be posted",
    }


def _setup(tmp_path, proposal_records, verdicts):
    dirs = {
        "decisions_dir": tmp_path / "decisions",
        "proposals_dir": tmp_path / "proposals",
        "results_dir": tmp_path / "results",
    }
    jsonl_log.append_weekly(proposal_records, dirs["proposals_dir"])
    decision_records = [
        decisions.record(p, verdict)
        for p, verdict in zip(proposal_records, verdicts)
    ]
    jsonl_log.append_weekly(decision_records, dirs["decisions_dir"])
    con = db.connect(":memory:")
    for p in proposal_records:
        con.execute(
            "INSERT OR IGNORE INTO issues (repo, number, title, state, updated_at,"
            " created_at, labels_json) VALUES (?,?,?,?,?,?,?)",
            (p["repo"], p["issue"], "t", "OPEN", UPDATED, UPDATED, "[]"),
        )
    return con, dirs


def _fake(issues=None):
    base = {"labels": [], "state": "open", "state_reason": None,
            "updated_at": UPDATED, "node_id": "N1"}
    return FakeGh(issues or {("o/r", 1): base})


def test_dry_run_writes_records_and_never_mutates(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], ["approved"]
    )
    gh = _fake()
    lines = []
    summary = executor.execute(
        con, run_gh=gh, apply=False, pace=lambda s: None, log=lines.append, **dirs
    )
    assert summary["counts"] == {"applied": 0, "dry-run": 1,
                                 "stale-needs-rereview": 0, "error": 0}
    assert gh.mutating_calls == []
    [rec] = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert rec["status"] == "dry-run"
    assert rec["batch_id"] == summary["batch_id"]
    assert rec["prior"] == {"labels": [], "state": "open", "state_reason": None}
    assert any("add-label" in line for line in lines)


def test_apply_add_label_updates_github_results_and_mirror(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], ["approved"]
    )
    gh = _fake()
    executor.execute(con, run_gh=gh, apply=True, pace=lambda s: None,
                     log=lambda *a: None, **dirs)
    assert gh.issues[("o/r", 1)]["labels"] == ["regression"]
    [rec] = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert rec["status"] == "applied"
    row = db.get_issue(con, "o/r", 1)
    assert json.loads(row["labels_json"]) == ["regression"]


def test_apply_close_posts_template_comment_then_closes(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "close", {"reason": "fixed"})], ["approved"]
    )
    gh = _fake()
    executor.execute(con, run_gh=gh, apply=True, pace=lambda s: None,
                     log=lambda *a: None, **dirs)
    issue = gh.issues[("o/r", 1)]
    assert issue["state"] == "closed" and issue["state_reason"] == "completed"
    [comment] = gh.comments.values()
    assert "reopen" in comment["body"]
    assert "model text" not in comment["body"]  # rationale never posted
    [rec] = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert rec["comment_id"] in gh.comments
    row = db.get_issue(con, "o/r", 1)
    assert row["state"] == "CLOSED" and row["state_reason"] == "COMPLETED"


def test_apply_close_duplicate_uses_graphql_duplicate_close(tmp_path):
    con, dirs = _setup(
        tmp_path,
        [_proposal("p1", "close-duplicate",
                   {"canonical": "o/r#2", "cross_repo_option": None})],
        ["approved"],
    )
    gh = _fake({
        ("o/r", 1): {"labels": [], "state": "open", "state_reason": None,
                     "updated_at": UPDATED, "node_id": "N1"},
        ("o/r", 2): {"labels": [], "state": "open", "state_reason": None,
                     "updated_at": UPDATED, "node_id": "N2"},
    })
    executor.execute(con, run_gh=gh, apply=True, pace=lambda s: None,
                     log=lambda *a: None, **dirs)
    issue = gh.issues[("o/r", 1)]
    assert issue["state"] == "closed" and issue["state_reason"] == "duplicate"
    row = db.get_issue(con, "o/r", 1)
    assert row["state"] == "CLOSED" and row["state_reason"] == "DUPLICATE"


def test_stale_issue_bounces_without_mutation(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "close", {"reason": "fixed"})], ["approved"]
    )
    gh = _fake({("o/r", 1): {"labels": [], "state": "open", "state_reason": None,
                             "updated_at": "2026-07-12T09:00:00Z", "node_id": "N1"}})
    summary = executor.execute(con, run_gh=gh, apply=True, pace=lambda s: None,
                               log=lambda *a: None, **dirs)
    assert summary["counts"]["stale-needs-rereview"] == 1
    assert gh.mutating_calls == []
    [rec] = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert rec["status"] == "stale-needs-rereview"


def test_error_records_continue_the_batch(tmp_path):
    con, dirs = _setup(
        tmp_path,
        [
            _proposal("p1", "add-label", {"label": "evil"}),
            _proposal("p2", "add-label", {"label": "regression"}, issue=2),
        ],
        ["approved", "approved"],
    )
    gh = _fake({
        ("o/r", 1): {"labels": [], "state": "open", "state_reason": None,
                     "updated_at": UPDATED, "node_id": "N1"},
        ("o/r", 2): {"labels": [], "state": "open", "state_reason": None,
                     "updated_at": UPDATED, "node_id": "N2"},
    })
    summary = executor.execute(con, run_gh=gh, apply=True, pace=lambda s: None,
                               log=lambda *a: None, **dirs)
    assert summary["counts"]["error"] == 1
    assert summary["counts"]["applied"] == 1
    assert gh.issues[("o/r", 2)]["labels"] == ["regression"]


def test_rerun_after_apply_skips_finalized_decisions(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], ["approved"]
    )
    gh = _fake()
    executor.execute(con, run_gh=gh, apply=True, pace=lambda s: None,
                     log=lambda *a: None, **dirs)
    first_mutations = len(gh.mutating_calls)
    summary = executor.execute(con, run_gh=gh, apply=True, pace=lambda s: None,
                               log=lambda *a: None, **dirs)
    assert len(gh.mutating_calls) == first_mutations
    assert summary["counts"] == {"applied": 0, "dry-run": 0,
                                 "stale-needs-rereview": 0, "error": 0}


def test_repo_filter_and_limit(tmp_path):
    con, dirs = _setup(
        tmp_path,
        [
            _proposal("p1", "add-label", {"label": "regression"}),
            _proposal("p2", "add-label", {"label": "duplicate"}, issue=2),
        ],
        ["approved", "approved"],
    )
    gh = _fake({
        ("o/r", 1): {"labels": [], "state": "open", "state_reason": None,
                     "updated_at": UPDATED, "node_id": "N1"},
        ("o/r", 2): {"labels": [], "state": "open", "state_reason": None,
                     "updated_at": UPDATED, "node_id": "N2"},
    })
    summary = executor.execute(con, run_gh=gh, apply=False, repo="other/repo",
                               pace=lambda s: None, log=lambda *a: None, **dirs)
    assert sum(summary["counts"].values()) == 0
    summary = executor.execute(con, run_gh=gh, apply=False, limit=1,
                               pace=lambda s: None, log=lambda *a: None, **dirs)
    assert sum(summary["counts"].values()) == 1
```

Note: `db.connect(":memory:")` + direct `INSERT INTO issues` — check `db.py`'s schema (`src/triage_verse/db.py:20-40`) when writing this; if the insert fails on NOT NULL columns, add the missing columns with dummy values rather than changing the schema. If `db.connect` doesn't accept `":memory:"`, use `db.connect(tmp_path / "m.sqlite")`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_executor_execute.py -q`
Expected: FAIL — `AttributeError: module 'triage_verse.executor' has no attribute 'execute'`

- [ ] **Step 4: Append the execute slice to `src/triage_verse/executor.py`**

Extend imports at the top of the file:

```python
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import jsonl_log, prompts, review_queue
from . import templates as templates_mod

RunGh = Callable[..., str]
```

Then append:

```python
def _fetch_issue(run_gh: RunGh, repo: str, number: int) -> dict:
    return json.loads(run_gh(["api", f"repos/{repo}/issues/{number}"]))


def _prior(issue: dict) -> dict:
    return {
        "labels": [entry["name"] for entry in issue.get("labels", [])],
        "state": issue["state"],
        "state_reason": issue.get("state_reason"),
    }


def _describe(mutation: dict) -> str:
    kind = mutation["kind"]
    if kind in ("add-label", "remove-label"):
        return f"{kind} {mutation['label']!r}"
    if kind == "comment":
        first_line = mutation["body"].strip().splitlines()[0]
        return f"comment: {first_line[:60]}…"
    if kind == "close":
        return f"close --reason {mutation['reason']!r}"
    return f"close as duplicate of {mutation['canonical'][0]}#{mutation['canonical'][1]}"


def _apply_mutation(
    run_gh: RunGh, repo: str, number: int, node_id: str, mutation: dict
) -> int | None:
    """Perform one mutation; returns the created comment id, if any."""
    kind = mutation["kind"]
    if kind == "add-label":
        run_gh(["issue", "edit", str(number), "--repo", repo,
                "--add-label", mutation["label"]])
    elif kind == "remove-label":
        run_gh(["issue", "edit", str(number), "--repo", repo,
                "--remove-label", mutation["label"]])
    elif kind == "comment":
        out = run_gh(["api", f"repos/{repo}/issues/{number}/comments",
                      "-f", f"body={mutation['body']}"])
        return json.loads(out)["id"]
    elif kind == "close":
        run_gh(["issue", "close", str(number), "--repo", repo,
                "--reason", mutation["reason"]])
    elif kind == "close-duplicate":
        dup_repo, dup_number = mutation["canonical"]
        dup = _fetch_issue(run_gh, dup_repo, dup_number)
        query = (
            "mutation($issue: ID!, $dup: ID!) { closeIssue(input: {"
            "issueId: $issue, stateReason: DUPLICATE, duplicateIssueId: $dup"
            "}) { issue { id } } }"
        )
        run_gh(["api", "graphql", "-f", f"query={query}",
                "-f", f"issue={node_id}", "-f", f"dup={dup['node_id']}"])
    return None


_MIRROR_STATE_REASON = {"completed": "COMPLETED", "not planned": "NOT_PLANNED"}


def _update_mirror(
    con: sqlite3.Connection, repo: str, number: int,
    prior_labels: list[str], mutations: list[dict],
) -> None:
    labels = list(prior_labels)
    state: str | None = None
    state_reason: str | None = None
    for m in mutations:
        if m["kind"] == "add-label" and m["label"] not in labels:
            labels.append(m["label"])
        elif m["kind"] == "remove-label" and m["label"] in labels:
            labels.remove(m["label"])
        elif m["kind"] == "close":
            state, state_reason = "CLOSED", _MIRROR_STATE_REASON[m["reason"]]
        elif m["kind"] == "close-duplicate":
            state, state_reason = "CLOSED", "DUPLICATE"
    if state is None:
        con.execute(
            "UPDATE issues SET labels_json=? WHERE repo=? AND number=?",
            (json.dumps(labels), repo, number),
        )
    else:
        con.execute(
            "UPDATE issues SET labels_json=?, state=?, state_reason=?"
            " WHERE repo=? AND number=?",
            (json.dumps(labels), state, state_reason, repo, number),
        )
    con.commit()


def execute(
    con: sqlite3.Connection,
    *,
    decisions_dir: Any,
    proposals_dir: Any,
    results_dir: Any,
    run_gh: RunGh,
    apply: bool = False,
    repo: str | None = None,
    limit: int | None = None,
    labels_path: str = ".github/triage/labels.yaml",
    templates_dir: Any = templates_mod.DEFAULT_DIR,
    pace: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict:
    """Apply approved decisions to GitHub (dry-run unless apply=True)."""
    tmpl = templates_mod.load(templates_dir)
    allowed = prompts.allowed_labels(labels_path)
    proposals_index = index_proposals(review_queue.iter_jsonl_records(proposals_dir))
    results = review_queue.iter_jsonl_records(results_dir)
    picked = select_executable(review_queue.iter_jsonl_records(decisions_dir), results)
    if repo is not None:
        picked = [d for d in picked if d["repo"] == repo]
    if limit is not None:
        picked = picked[:limit]

    batch_id = uuid.uuid4().hex
    counts = {"applied": 0, "dry-run": 0, "stale-needs-rereview": 0, "error": 0}
    records: list[dict] = []
    first_mutation = True

    for decision in picked:
        rec = {
            "id": uuid.uuid4().hex,
            "batch_id": batch_id,
            "decision_id": decision["id"],
            "proposal_id": decision["proposal_id"],
            "repo": decision["repo"],
            "issue": decision["issue"],
            "action": decision["action"],
            "params": decision.get("params") or {},
            "executed_at": _now(),
        }
        proposal = proposals_index.get(decision["proposal_id"])
        if proposal is None:
            rec.update(status="error", error="proposal not found")
            records.append(rec)
            counts["error"] += 1
            continue
        try:
            issue = _fetch_issue(run_gh, decision["repo"], decision["issue"])
        except Exception as exc:  # gh.GhError or JSON decode
            rec.update(status="error", error=f"fetch failed: {exc}")
            records.append(rec)
            counts["error"] += 1
            continue
        rec["prior"] = _prior(issue)
        if issue["updated_at"] != proposal.get("issue_updated_at"):
            rec["status"] = "stale-needs-rereview"
            log(
                f"STALE {decision['repo']}#{decision['issue']}: updated_at moved "
                f"{proposal.get('issue_updated_at')} -> {issue['updated_at']}"
            )
            records.append(rec)
            counts["stale-needs-rereview"] += 1
            continue
        mutations, err = plan_decision(decision, issue, allowed=allowed, tmpl=tmpl)
        if err is not None:
            rec.update(status="error", error=err)
            log(f"ERROR {decision['repo']}#{decision['issue']}: {err}")
            records.append(rec)
            counts["error"] += 1
            continue
        header = f"{decision['repo']}#{decision['issue']} [{decision['action']}]"
        if not apply:
            for m in mutations:
                log(f"DRY-RUN {header}: {_describe(m)}")
            rec["status"] = "dry-run"
            records.append(rec)
            counts["dry-run"] += 1
            continue
        try:
            for m in mutations:
                if not first_mutation:
                    pace(1.0)
                first_mutation = False
                log(f"APPLY {header}: {_describe(m)}")
                comment_id = _apply_mutation(
                    run_gh, decision["repo"], decision["issue"],
                    issue["node_id"], m,
                )
                if comment_id is not None:
                    rec["comment_id"] = comment_id
        except Exception as exc:
            rec.update(status="error", error=str(exc))
            records.append(rec)
            counts["error"] += 1
            continue
        _update_mirror(
            con, decision["repo"], decision["issue"], rec["prior"]["labels"], mutations
        )
        rec["status"] = "applied"
        records.append(rec)
        counts["applied"] += 1

    if records:
        jsonl_log.append_weekly(records, results_dir)
    log(f"batch {batch_id}: {counts}")
    return {"batch_id": batch_id, "counts": counts}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_executor_execute.py -q`
Expected: 8 passed. If the `INSERT INTO issues` fixture fails on schema mismatch, fix the test fixture (not the schema).

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest tests/ -q`
Expected: all passed (170 pre-existing + new).

- [ ] **Step 7: Commit**

```bash
git add src/triage_verse/executor.py tests/triage_verse/fake_gh.py tests/triage_verse/test_executor_execute.py
git commit -m "feat(executor): execute approved decisions with freshness check and results log"
```

---

### Task 5: `executor.undo` — reverse an executed batch

**Files:**
- Modify: `src/triage_verse/executor.py`
- Test: `tests/triage_verse/test_executor_undo.py`

**Interfaces:**
- Consumes: Task 4 (`execute`, result records, `FakeGh`, `_apply_mutation` helpers).
- Produces (used by Task 6): `executor.undo(con, *, results_dir, batch_id, run_gh, issue=None, apply=False, pace=time.sleep, log=print) -> dict` returning `{"batch_id": <new undo batch id>, "counts": {"applied": …, "dry-run": …, "error": …, "skipped": …}}`. `issue` is a string like `"owner/name#N"`. Undo result records carry `action: "undo"` and `undoes_result_id`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_executor_undo.py
"""Round-trip tests for executor.undo."""

import json

from triage_verse import db, decisions, executor, jsonl_log, review_queue

from .fake_gh import FakeGh

UPDATED = "2026-07-01T00:00:00Z"


def _proposal(pid, action, params, issue=1):
    return {
        "id": pid, "repo": "o/r", "issue": issue, "issue_updated_at": UPDATED,
        "run_id": "run1", "model": "m", "confidence": 0.9, "evidence": [],
        "action": action, "params": params, "rationale": "",
    }


def _issue(labels=(), node="N1"):
    return {"labels": list(labels), "state": "open", "state_reason": None,
            "updated_at": UPDATED, "node_id": node}


def _run_batch(tmp_path, proposal_records, gh):
    dirs = {
        "decisions_dir": tmp_path / "decisions",
        "proposals_dir": tmp_path / "proposals",
        "results_dir": tmp_path / "results",
    }
    jsonl_log.append_weekly(proposal_records, dirs["proposals_dir"])
    jsonl_log.append_weekly(
        [decisions.record(p, "approved") for p in proposal_records],
        dirs["decisions_dir"],
    )
    con = db.connect(":memory:")
    for p in proposal_records:
        con.execute(
            "INSERT OR IGNORE INTO issues (repo, number, title, state, updated_at,"
            " created_at, labels_json) VALUES (?,?,?,?,?,?,?)",
            (p["repo"], p["issue"], "t", "OPEN", UPDATED, UPDATED, "[]"),
        )
    summary = executor.execute(con, run_gh=gh, apply=True, pace=lambda s: None,
                               log=lambda *a: None, **dirs)
    return con, dirs, summary["batch_id"]


def test_undo_round_trip_restores_labels_state_and_comments(tmp_path):
    gh = FakeGh({
        ("o/r", 1): _issue(labels=["Priority: Low", "bug"]),
        ("o/r", 2): _issue(node="N2"),
    })
    con, dirs, batch_id = _run_batch(
        tmp_path,
        [
            _proposal("p1", "set-priority", {"priority": "High"}),
            _proposal("p2", "close", {"reason": "fixed"}, issue=2),
        ],
        gh,
    )
    assert gh.issues[("o/r", 1)]["labels"] == ["bug", "Priority: High"]
    assert gh.issues[("o/r", 2)]["state"] == "closed"
    assert len(gh.comments) == 1

    summary = executor.undo(
        con, results_dir=dirs["results_dir"], batch_id=batch_id,
        run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None,
    )
    assert summary["counts"]["applied"] == 2
    assert sorted(gh.issues[("o/r", 1)]["labels"]) == ["Priority: Low", "bug"]
    assert gh.issues[("o/r", 2)]["state"] == "open"
    assert gh.comments == {}
    row = db.get_issue(con, "o/r", 2)
    assert row["state"] == "OPEN" and row["state_reason"] is None


def test_undo_dry_run_by_default(tmp_path):
    gh = FakeGh({("o/r", 1): _issue()})
    con, dirs, batch_id = _run_batch(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], gh
    )
    before = len(gh.mutating_calls)
    summary = executor.undo(
        con, results_dir=dirs["results_dir"], batch_id=batch_id,
        run_gh=gh, pace=lambda s: None, log=lambda *a: None,
    )
    assert summary["counts"]["dry-run"] == 1
    assert len(gh.mutating_calls) == before
    assert gh.issues[("o/r", 1)]["labels"] == ["regression"]


def test_undo_is_idempotent(tmp_path):
    gh = FakeGh({("o/r", 1): _issue()})
    con, dirs, batch_id = _run_batch(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], gh
    )
    executor.undo(con, results_dir=dirs["results_dir"], batch_id=batch_id,
                  run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None)
    summary = executor.undo(
        con, results_dir=dirs["results_dir"], batch_id=batch_id,
        run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None,
    )
    assert summary["counts"]["applied"] == 0
    assert summary["counts"]["skipped"] == 1


def test_undo_does_not_remove_preexisting_label(tmp_path):
    # add-label on an issue that already carried the label: undo must not strip it.
    gh = FakeGh({("o/r", 1): _issue(labels=["regression"])})
    con, dirs, batch_id = _run_batch(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], gh
    )
    executor.undo(con, results_dir=dirs["results_dir"], batch_id=batch_id,
                  run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None)
    assert gh.issues[("o/r", 1)]["labels"] == ["regression"]


def test_undo_issue_filter(tmp_path):
    gh = FakeGh({("o/r", 1): _issue(), ("o/r", 2): _issue(node="N2")})
    con, dirs, batch_id = _run_batch(
        tmp_path,
        [
            _proposal("p1", "add-label", {"label": "regression"}),
            _proposal("p2", "add-label", {"label": "duplicate"}, issue=2),
        ],
        gh,
    )
    executor.undo(con, results_dir=dirs["results_dir"], batch_id=batch_id,
                  run_gh=gh, issue="o/r#1", apply=True,
                  pace=lambda s: None, log=lambda *a: None)
    assert gh.issues[("o/r", 1)]["labels"] == []
    assert gh.issues[("o/r", 2)]["labels"] == ["duplicate"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_executor_undo.py -q`
Expected: FAIL — `AttributeError: module 'triage_verse.executor' has no attribute 'undo'`

- [ ] **Step 3: Append the undo slice to `src/triage_verse/executor.py`**

```python
def _reverse_mutations(rec: dict) -> list[dict]:
    """Mutations that reverse one applied result record."""
    action = rec["action"]
    params = rec.get("params") or {}
    prior_labels = rec.get("prior", {}).get("labels", [])
    muts: list[dict] = []
    if action == "add-label":
        label = params.get("label")
        if label and label not in prior_labels:
            muts.append({"kind": "remove-label", "label": label})
    elif action == "set-priority":
        label = f"Priority: {params.get('priority')}"
        if label not in prior_labels:
            muts.append({"kind": "remove-label", "label": label})
        muts.extend(
            {"kind": "add-label", "label": name}
            for name in prior_labels
            if name.startswith("Priority: ") and name != label
        )
    elif action in ("close", "close-duplicate"):
        if rec.get("comment_id") is not None:
            muts.append({"kind": "delete-comment", "comment_id": rec["comment_id"]})
        muts.append({"kind": "reopen"})
    return muts


def _apply_reverse(run_gh: RunGh, repo: str, number: int, mutation: dict) -> None:
    kind = mutation["kind"]
    if kind in ("add-label", "remove-label"):
        _apply_mutation(run_gh, repo, number, "", mutation)
    elif kind == "delete-comment":
        run_gh(["api", "-X", "DELETE",
                f"repos/{repo}/issues/comments/{mutation['comment_id']}"])
    elif kind == "reopen":
        run_gh(["issue", "reopen", str(number), "--repo", repo])


def _describe_reverse(mutation: dict) -> str:
    if mutation["kind"] == "delete-comment":
        return f"delete comment {mutation['comment_id']}"
    if mutation["kind"] == "reopen":
        return "reopen"
    return _describe(mutation)


def _undo_mirror(con: sqlite3.Connection, rec: dict) -> None:
    prior = rec.get("prior")
    if not prior:
        return
    con.execute(
        "UPDATE issues SET labels_json=?, state=?, state_reason=?"
        " WHERE repo=? AND number=?",
        (
            json.dumps(prior["labels"]),
            prior["state"].upper(),
            prior["state_reason"].upper() if prior.get("state_reason") else None,
            rec["repo"],
            rec["issue"],
        ),
    )
    con.commit()


def undo(
    con: sqlite3.Connection,
    *,
    results_dir: Any,
    batch_id: str,
    run_gh: RunGh,
    issue: str | None = None,
    apply: bool = False,
    pace: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict:
    """Reverse an executed batch (dry-run unless apply=True)."""
    all_results = review_queue.iter_jsonl_records(results_dir)
    already_undone = {
        r["undoes_result_id"]
        for r in all_results
        if r.get("action") == "undo" and r.get("status") == "applied"
    }
    targets = [
        r
        for r in all_results
        if r.get("batch_id") == batch_id
        and r.get("status") == "applied"
        and r.get("action") != "undo"
    ]
    if issue is not None:
        ref = parse_issue_ref(issue, default_repo="")
        if ref is None:
            raise ValueError(f"cannot parse --issue value: {issue!r}")
        targets = [r for r in targets if (r["repo"], r["issue"]) == ref]

    undo_batch_id = uuid.uuid4().hex
    counts = {"applied": 0, "dry-run": 0, "error": 0, "skipped": 0}
    records: list[dict] = []
    first_mutation = True

    for rec in reversed(targets):
        header = f"{rec['repo']}#{rec['issue']} [undo {rec['action']}]"
        if rec["id"] in already_undone:
            log(f"SKIP {header}: already undone")
            counts["skipped"] += 1
            continue
        out = {
            "id": uuid.uuid4().hex,
            "batch_id": undo_batch_id,
            "undoes_result_id": rec["id"],
            "action": "undo",
            "repo": rec["repo"],
            "issue": rec["issue"],
            "params": {"undone_action": rec["action"]},
            "executed_at": _now(),
        }
        mutations = _reverse_mutations(rec)
        if not apply:
            for m in mutations:
                log(f"DRY-RUN {header}: {_describe_reverse(m)}")
            out["status"] = "dry-run"
            records.append(out)
            counts["dry-run"] += 1
            continue
        try:
            for m in mutations:
                if not first_mutation:
                    pace(1.0)
                first_mutation = False
                log(f"APPLY {header}: {_describe_reverse(m)}")
                _apply_reverse(run_gh, rec["repo"], rec["issue"], m)
        except Exception as exc:
            out.update(status="error", error=str(exc))
            records.append(out)
            counts["error"] += 1
            continue
        _undo_mirror(con, rec)
        out["status"] = "applied"
        records.append(out)
        counts["applied"] += 1

    if records:
        jsonl_log.append_weekly(records, results_dir)
    log(f"undo batch {undo_batch_id} (undoes {batch_id}): {counts}")
    return {"batch_id": undo_batch_id, "counts": counts}
```

Note: `_undo_mirror` restores from `prior` (REST lowercase, e.g. `"open"` → mirror `"OPEN"`); `state_reason` `None` stays `None` — the round-trip test asserts this.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_executor_undo.py tests/triage_verse/test_executor_execute.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/executor.py tests/triage_verse/test_executor_undo.py
git commit -m "feat(executor): batch undo with per-issue filter and idempotency"
```

---

### Task 6: CLI wiring — `triage-verse execute` / `triage-verse undo`

**Files:**
- Modify: `src/triage_verse/cli.py`
- Test: `tests/triage_verse/test_cli_execute.py`

**Interfaces:**
- Consumes: `executor.execute`, `executor.undo`, `gh.run_gh`.
- Produces: CLI subcommands. Defaults honor env vars: `TRIAGE_VERSE_DECISIONS` (`.data/decisions`), `TRIAGE_VERSE_PROPOSALS` (`.data/proposals`), `TRIAGE_VERSE_RESULTS` (`.data/results`), `TRIAGE_VERSE_DB` (`.data/mirror.sqlite`). Exit code 1 if any record ended `error`, else 0.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_cli_execute.py
"""CLI wiring tests for execute/undo (executor functions monkeypatched)."""

from triage_verse import cli, executor


def test_execute_defaults_to_dry_run_and_env_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGE_VERSE_DECISIONS", str(tmp_path / "d"))
    monkeypatch.setenv("TRIAGE_VERSE_DB", str(tmp_path / "m.sqlite"))
    seen = {}

    def fake_execute(con, **kwargs):
        seen.update(kwargs)
        return {"batch_id": "b1", "counts": {"applied": 0, "dry-run": 2,
                                             "stale-needs-rereview": 0, "error": 0}}

    monkeypatch.setattr(executor, "execute", fake_execute)
    rc = cli.main(["execute"])
    assert rc == 0
    assert seen["apply"] is False
    assert seen["decisions_dir"] == str(tmp_path / "d")
    assert seen["proposals_dir"] == ".data/proposals"
    assert seen["run_gh"] is not None


def test_execute_apply_flag_and_error_exit_code(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGE_VERSE_DB", str(tmp_path / "m.sqlite"))
    seen = {}

    def fake_execute(con, **kwargs):
        seen.update(kwargs)
        return {"batch_id": "b1", "counts": {"applied": 1, "dry-run": 0,
                                             "stale-needs-rereview": 0, "error": 1}}

    monkeypatch.setattr(executor, "execute", fake_execute)
    rc = cli.main(["execute", "--apply", "--repo", "o/r", "--limit", "5"])
    assert rc == 1
    assert seen["apply"] is True and seen["repo"] == "o/r" and seen["limit"] == 5


def test_undo_requires_batch_and_passes_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGE_VERSE_DB", str(tmp_path / "m.sqlite"))
    seen = {}

    def fake_undo(con, **kwargs):
        seen.update(kwargs)
        return {"batch_id": "u1", "counts": {"applied": 0, "dry-run": 1,
                                             "error": 0, "skipped": 0}}

    monkeypatch.setattr(executor, "undo", fake_undo)
    rc = cli.main(["undo", "--batch", "abc123", "--issue", "o/r#7"])
    assert rc == 0
    assert seen["batch_id"] == "abc123"
    assert seen["issue"] == "o/r#7"
    assert seen["apply"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_cli_execute.py -q`
Expected: FAIL — argparse error `invalid choice: 'execute'` (SystemExit).

- [ ] **Step 3: Wire the subcommands into `src/triage_verse/cli.py`**

Add imports near the other module imports (`import os` alongside stdlib imports; the executor/gh imports with the relative ones):

```python
import os

from . import executor as executor_mod
from . import gh
```

Add the handlers (note: they call `executor_mod.execute` / `executor_mod.undo` via module attribute so tests can monkeypatch):

```python
def _cmd_execute(args: argparse.Namespace) -> int:
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
        repo=args.repo,
        limit=args.limit,
    )
    return 1 if summary["counts"]["error"] else 0


def _cmd_undo(args: argparse.Namespace) -> int:
    con = _open_db(args.db)
    summary = executor_mod.undo(
        con,
        results_dir=args.results_dir,
        batch_id=args.batch,
        issue=args.issue,
        run_gh=gh.run_gh,
        apply=args.apply,
    )
    return 1 if summary["counts"]["error"] else 0
```

In `build_parser()`, add before `return parser`. **Important:** env-var defaults must be read at parse time, not import time, so use `default=None` + resolve in the parser via a small helper — otherwise monkeypatch.setenv in tests (and real shell exports) won't be honored if another test imported cli first. Simplest correct form:

```python
    p_exec = sub.add_parser("execute", help="apply approved decisions (dry-run by default)")
    p_exec.add_argument("--db", default=None)
    p_exec.add_argument("--decisions-dir", default=None)
    p_exec.add_argument("--proposals-dir", default=None)
    p_exec.add_argument("--results-dir", default=None)
    p_exec.add_argument("--labels", default=".github/triage/labels.yaml")
    p_exec.add_argument("--templates", default="config/templates")
    p_exec.add_argument("--repo", help="only decisions for this owner/name")
    p_exec.add_argument("--limit", type=int, help="max decisions this run")
    p_exec.add_argument("--apply", action="store_true",
                        help="perform mutations (default: dry-run)")
    p_exec.set_defaults(func=_cmd_execute)

    p_undo = sub.add_parser("undo", help="reverse an executed batch (dry-run by default)")
    p_undo.add_argument("--db", default=None)
    p_undo.add_argument("--results-dir", default=None)
    p_undo.add_argument("--batch", required=True, help="batch id to reverse")
    p_undo.add_argument("--issue", help="restrict to one issue, e.g. owner/name#7")
    p_undo.add_argument("--apply", action="store_true",
                        help="perform mutations (default: dry-run)")
    p_undo.set_defaults(func=_cmd_undo)
```

And resolve the env-var defaults at the top of both handlers (before use):

```python
def _env_default(value: str | None, env: str, fallback: str) -> str:
    return value if value is not None else os.environ.get(env, fallback)
```

In `_cmd_execute`:

```python
    args.db = _env_default(args.db, "TRIAGE_VERSE_DB", DEFAULT_DB)
    args.decisions_dir = _env_default(args.decisions_dir, "TRIAGE_VERSE_DECISIONS", ".data/decisions")
    args.proposals_dir = _env_default(args.proposals_dir, "TRIAGE_VERSE_PROPOSALS", DEFAULT_PROPOSALS)
    args.results_dir = _env_default(args.results_dir, "TRIAGE_VERSE_RESULTS", ".data/results")
```

In `_cmd_undo`:

```python
    args.db = _env_default(args.db, "TRIAGE_VERSE_DB", DEFAULT_DB)
    args.results_dir = _env_default(args.results_dir, "TRIAGE_VERSE_RESULTS", ".data/results")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_cli_execute.py tests/triage_verse/test_cli.py -q`
Expected: all passed

- [ ] **Step 5: Smoke the real CLI (no mutations — dry-run against empty dirs)**

Run: `uv run triage-verse execute --decisions-dir /tmp/nope --proposals-dir /tmp/nope --results-dir /tmp/nope-results --db /tmp/smoke.sqlite`
Expected: prints `batch <id>: {'applied': 0, 'dry-run': 0, 'stale-needs-rereview': 0, 'error': 0}`, exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/cli.py tests/triage_verse/test_cli_execute.py
git commit -m "feat(executor): triage-verse execute/undo CLI, dry-run by default"
```

---

### Task 7: Stale resurfacing in the review queue + app badge

**Files:**
- Modify: `src/triage_verse/review_queue.py` (`load_undecided`)
- Modify: `src/triage_verse/review_app/app.py` (env var, call site, badge)
- Test: `tests/triage_verse/test_review_queue.py` (append tests)

**Interfaces:**
- Consumes: result records with `status: "stale-needs-rereview"` (Task 4).
- Produces: `review_queue.load_undecided(proposals_dir, decisions_dir, con, results_dir=None)` — proposals whose latest event is a stale result resurface with `proposal["stale"] = True`.

- [ ] **Step 1: Write the failing tests (append to `tests/triage_verse/test_review_queue.py`)**

Read the top of the existing file first and reuse its fixtures/helpers for writing proposals/decisions JSONL and the in-memory db. The tests to add (adapt helper names to what exists):

```python
def test_stale_result_resurfaces_proposal(tmp_path):
    con = _con_with_open_issue("o/r", 1)  # adapt to existing helper
    proposal = {"id": "p1", "repo": "o/r", "issue": 1, "action": "add-label",
                "params": {"label": "regression"}, "confidence": 0.9}
    jsonl_log.append_weekly([proposal], tmp_path / "proposals")
    jsonl_log.append_weekly(
        [{"id": "d1", "proposal_id": "p1", "verdict": "approved",
          "decided_at": "2026-07-12T00:00:00Z"}],
        tmp_path / "decisions",
    )
    jsonl_log.append_weekly(
        [{"id": "r1", "proposal_id": "p1", "status": "stale-needs-rereview",
          "executed_at": "2026-07-13T00:00:00Z"}],
        tmp_path / "results",
    )
    # Without results_dir: hidden (decided). With: resurfaces, flagged stale.
    assert review_queue.load_undecided(
        tmp_path / "proposals", tmp_path / "decisions", con
    ) == []
    [row] = review_queue.load_undecided(
        tmp_path / "proposals", tmp_path / "decisions", con,
        results_dir=tmp_path / "results",
    )
    assert row["id"] == "p1" and row["stale"] is True


def test_fresh_decision_after_stale_result_hides_again(tmp_path):
    con = _con_with_open_issue("o/r", 1)
    proposal = {"id": "p1", "repo": "o/r", "issue": 1, "action": "add-label",
                "params": {"label": "regression"}, "confidence": 0.9}
    jsonl_log.append_weekly([proposal], tmp_path / "proposals")
    jsonl_log.append_weekly(
        [{"id": "r1", "proposal_id": "p1", "status": "stale-needs-rereview",
          "executed_at": "2026-07-13T00:00:00Z"}],
        tmp_path / "results",
    )
    jsonl_log.append_weekly(
        [{"id": "d2", "proposal_id": "p1", "verdict": "rejected",
          "decided_at": "2026-07-14T00:00:00Z"}],
        tmp_path / "decisions",
    )
    assert review_queue.load_undecided(
        tmp_path / "proposals", tmp_path / "decisions", con,
        results_dir=tmp_path / "results",
    ) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -q`
Expected: new tests FAIL — `TypeError: load_undecided() got an unexpected keyword argument 'results_dir'`

- [ ] **Step 3: Extend `load_undecided` in `src/triage_verse/review_queue.py`**

Replace the existing function body with:

```python
def load_undecided(
    proposals_dir: str | pathlib.Path,
    decisions_dir: str | pathlib.Path,
    con: sqlite3.Connection,
    results_dir: str | pathlib.Path | None = None,
) -> list[dict]:
    stale_at: dict[str, str] = {}
    if results_dir is not None:
        for r in iter_jsonl_records(results_dir):
            if r.get("status") == "stale-needs-rereview" and r.get("proposal_id"):
                t = r.get("executed_at", "")
                if t > stale_at.get(r["proposal_id"], ""):
                    stale_at[r["proposal_id"]] = t
    latest_decided: dict[str, str] = {}
    for r in iter_jsonl_records(decisions_dir):
        if "proposal_id" not in r:
            continue
        t = r.get("decided_at", "")
        if t >= latest_decided.get(r["proposal_id"], ""):
            latest_decided[r["proposal_id"]] = t
    decided_ids = {
        pid
        for pid, t in latest_decided.items()
        # A newer stale bounce voids the decision; the proposal resurfaces.
        if stale_at.get(pid, "") <= t
    }
    proposals = [
        {**r, "stale": True} if r.get("id") in stale_at else r
        for r in iter_jsonl_records(proposals_dir)
        if r.get("id") not in decided_ids
        and r.get("action") in SUPPORTED_ACTIONS
        and not _is_closed(con, r["repo"], r["issue"])
    ]
    return sorted(proposals, key=lambda r: r.get("confidence", 0.0), reverse=True)
```

- [ ] **Step 4: Wire the app (`src/triage_verse/review_app/app.py`)**

Next to the other dir constants (`app.py:18-20`):

```python
RESULTS_DIR = os.environ.get("TRIAGE_VERSE_RESULTS", ".data/results")
```

Update both `load_undecided` call sites (`app.py:400` and `app.py:408`) to pass `results_dir=RESULTS_DIR`.

In `row_ui` (`app.py:78`), after the existing high-stakes badge insert, add a stale badge:

```python
    if proposal.get("stale"):
        header.insert(
            0,
            ui.span(
                "stale",
                style=(
                    "background-color: #ef6c00; color: white; border-radius: 999px; "
                    "padding: 0 0.5rem; margin-right: 0.5rem; font-size: 0.8rem;"
                ),
                title="Issue changed on GitHub after this was proposed; re-review.",
            ),
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -q`
Expected: all passed (existing tests must still pass — the no-`results_dir` behavior is unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/review_queue.py src/triage_verse/review_app/app.py tests/triage_verse/test_review_queue.py
git commit -m "feat(review-app): resurface stale-bounced proposals with a stale badge"
```

---

### Task 8: Full check + docs

**Files:**
- Modify: `README.md` (add execute/undo to the CLI overview — read the existing CLI section and match its format)

**Interfaces:** none new.

- [ ] **Step 1: Update README**

Find the section listing `triage-verse` subcommands (search for `verify-counts` or `analyze`). Add two entries following the existing format:

- `execute` — apply approved review decisions to GitHub. **Dry-run by default; pass `--apply` to mutate.** Freshness-checked per issue; results append to `.data/results/`.
- `undo --batch <id>` — reverse an executed batch (labels restored, issues reopened, executor comments deleted). Also dry-run by default.

If the README has no subcommand list, add these under a short "Executor" heading after whatever section describes the review app.

- [ ] **Step 2: Run the full gate**

Run: `make check`
Expected: ruff, pyright, pytest, yaml validation all pass. Fix anything that fails (common: unused imports, pyright complaining about `dict` vs typed params — annotate as `dict[str, Any]` where needed).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document triage-verse execute/undo"
```

---

## Plan self-review (done at write time)

- **Spec coverage:** CLI surface (T6), input selection incl. proposal join + latest-wins + dry-run-not-final (T2, T4), freshness contract (T4), stale resurfacing + badge (T7), full mutation allowlist incl. reason mapping + cross-repo duplicate fallback (T3), templates with exact spec wording + placeholder validation (T1), results log schema + error-continues-batch (T4), undo incl. idempotency, pre-existing-label guard, per-issue filter, mirror restore (T5), mirror update after apply (T4), pacing injectable (T4/T5), all six spec test categories mapped: dry-run snapshot → T4, freshness bounce → T4+T7, undo round-trip → T5, allowlist matrix → T3, idempotency → T4/T5, latest-decision/edited-params → T2.
- **Not covered anywhere (deliberate):** graduated autonomy, App tokens, transfers — spec's out-of-scope list.
- **Type consistency check:** mutation dict vocabulary defined in T3 and consumed in T4/T5; `FakeGh` defined in T4, reused in T5; result-record field names identical across T4/T5/T7 (`proposal_id`, `status`, `executed_at`, `undoes_result_id`).
