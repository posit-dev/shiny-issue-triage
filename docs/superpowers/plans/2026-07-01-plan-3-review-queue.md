# Plan 3a Review Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a plain Shiny-for-Python review queue that lets a human approve, reject, or skip `add-label`/`set-priority` triage proposals, recording each decision to a local append-only log.

**Architecture:** Two new pure (Shiny-free) modules — `review_queue.py` (load undecided proposals, sorted by confidence) and `decisions.py` (record + append decisions) — plus a thin standalone Shiny app (`review_app/app.py`) that wires them to a per-row UI built from dynamic Shiny modules. A small shared `jsonl_log.py` helper is extracted first so `decisions.py` and the existing `proposals.py` don't duplicate the weekly-partitioned-JSONL-append logic.

**Tech Stack:** Python 3.11, `shiny` (Shiny for Python, new dependency), existing `sqlite3`-backed mirror (`db.py`), `pytest`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-01-plan-3-review-queue-design.md`.
- Scope is `add-label` and `set-priority` proposals only. Do not add handling for `close` or `close-duplicate` — they're explicitly deferred. Because Plan 2 writes all four action types into the same proposals JSONL files, `review_queue.load_undecided` must filter to `SUPPORTED_ACTIONS` itself — this isn't optional pre-filtering upstream.
- No drawer, no dashboard, no React frontend, no keyboard shortcuts, no edit action, no SQLite index of decided ids. Plain Shiny for Python, JSONL-file scanning only.
- Default paths (env-var overridable): `TRIAGE_VERSE_DB=.data/mirror.sqlite`, `TRIAGE_VERSE_PROPOSALS=.data/proposals`, `TRIAGE_VERSE_DECISIONS=.data/decisions`.
- Decision record shape: `{id, proposal_id, repo, issue, action, params, verdict, confidence, decided_at}`, `verdict` ∈ `{approved, rejected, skipped}`. No `reviewer` field.
- A proposal with *any* decision record (any verdict) must never reappear in the queue.
- Queue is a flat list (no grouping), sorted by `confidence` ascending.
- Follow the existing 1:1 `src/triage_verse/<name>.py` ↔ `tests/triage_verse/test_<name>.py` convention for every new pure module.
- Run `make py-check` (ruff format/lint, pyright, pytest) before each commit that touches Python source; it must stay green.

---

### Task 1: Extract shared weekly-JSONL append helper

**Files:**
- Create: `src/triage_verse/jsonl_log.py`
- Modify: `src/triage_verse/proposals.py` (its `write` function)
- Test: `tests/triage_verse/test_jsonl_log.py`

**Interfaces:**
- Produces: `jsonl_log.append_weekly(records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None) -> pathlib.Path` — appends each record as one JSON line to `<base_dir>/<ISO year>/W<ISO week>.jsonl`, creating parent dirs as needed, writing atomically (temp file + `replace`). `today` (an ISO date string) overrides "now" for deterministic tests; when omitted, uses today's real date.

- [ ] **Step 1: Write the failing test**

```python
# tests/triage_verse/test_jsonl_log.py
import json

from triage_verse import jsonl_log


def test_append_weekly_creates_partition_and_appends(tmp_path):
    recs = [{"a": 1}]
    path = jsonl_log.append_weekly(recs, tmp_path / "log", today="2026-06-29")
    assert path.exists()
    assert "2026/W27.jsonl" in str(path).replace("\\", "/")
    line = json.loads(path.read_text().splitlines()[0])
    assert line == {"a": 1}
    # appends, not overwrites
    jsonl_log.append_weekly(recs, tmp_path / "log", today="2026-06-29")
    assert len(path.read_text().splitlines()) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_jsonl_log.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'triage_verse.jsonl_log'`

- [ ] **Step 3: Write the implementation**

```python
# src/triage_verse/jsonl_log.py
"""Shared helper for appending records to weekly-partitioned JSONL logs."""

from __future__ import annotations

import json
import pathlib
from datetime import date


def append_weekly(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    day = date.fromisoformat(today) if today else date.today()
    year, week, _ = day.isocalendar()
    out = pathlib.Path(base_dir) / f"{year}" / f"W{week:02d}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = out.read_text(encoding="utf-8") if out.exists() else ""
    payload = existing + "".join(json.dumps(r) + "\n" for r in records)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(out)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/triage_verse/test_jsonl_log.py -v`
Expected: PASS

- [ ] **Step 5: Refactor `proposals.py` to use the shared helper**

Open `src/triage_verse/proposals.py`. Delete its `write` function body and the now-unused `from datetime import date` import, then replace with:

```python
from . import jsonl_log


def write(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    return jsonl_log.append_weekly(records, base_dir, today=today)
```

Place the `from . import jsonl_log` line with the other imports at the top of the file (alongside the existing `import json`, `import pathlib`, `import uuid`). Remove the old `from datetime import date` line — it's no longer used anywhere in this file.

- [ ] **Step 6: Run the full existing proposals test suite to verify no regression**

Run: `uv run pytest tests/triage_verse/test_proposals.py -v`
Expected: PASS (all 3 existing tests, unchanged behavior)

- [ ] **Step 7: Run full checks and commit**

Run: `make py-check`
Expected: all green

```bash
git add src/triage_verse/jsonl_log.py src/triage_verse/proposals.py tests/triage_verse/test_jsonl_log.py
git commit -m "refactor: extract shared weekly-JSONL append helper"
```

---

### Task 2: Add `db.get_issue` getter

**Files:**
- Modify: `src/triage_verse/db.py:221-223` (immediately after `upsert_issue`)
- Test: `tests/triage_verse/test_db.py`

**Interfaces:**
- Produces: `db.get_issue(con: sqlite3.Connection, repo: str, number: int) -> sqlite3.Row | None` — full row from the `issues` table, or `None` if not found.

**Consumes:** nothing new; follows the existing `get_classification`/`get_dedup_verdict` getter pattern already in this file (`db.py:315-320`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/triage_verse/test_db.py` (it already has an `_issue_row()` fixture helper near the top — reuse it, do not redefine):

```python
def test_get_issue_returns_row(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row())
    row = db.get_issue(con, "rstudio/shiny", 1)
    assert row["title"] == "first"


def test_get_issue_missing_returns_none(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert db.get_issue(con, "rstudio/shiny", 999) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_db.py -k get_issue -v`
Expected: FAIL with `AttributeError: module 'triage_verse.db' has no attribute 'get_issue'`

- [ ] **Step 3: Implement it**

In `src/triage_verse/db.py`, insert this function directly after `upsert_issue` (currently lines 221-222) and before `upsert_pr`:

```python
def get_issue(con: sqlite3.Connection, repo: str, number: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM issues WHERE repo=? AND number=?", (repo, number)
    ).fetchone()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_db.py -k get_issue -v`
Expected: PASS

- [ ] **Step 5: Run full checks and commit**

Run: `make py-check`

```bash
git add src/triage_verse/db.py tests/triage_verse/test_db.py
git commit -m "feat(db): add get_issue getter"
```

---

### Task 3: `review_queue.py` — load undecided proposals

**Files:**
- Create: `src/triage_verse/review_queue.py`
- Test: `tests/triage_verse/test_review_queue.py`

**Interfaces:**
- Produces:
  - `review_queue.SUPPORTED_ACTIONS: frozenset[str]` — `frozenset({"add-label", "set-priority"})`. The spec scopes this app to these two action types only; `close`/`close-duplicate` proposals exist in the same proposals JSONL files (Plan 2 emits all four types) but must never surface in this queue.
  - `review_queue.load_undecided(proposals_dir: str | pathlib.Path, decisions_dir: str | pathlib.Path) -> list[dict]` — every proposal record whose `action` is in `SUPPORTED_ACTIONS` and has no matching decision record (by `proposal["id"] == decision["proposal_id"]`), sorted by `confidence` ascending. Missing dirs return `[]`, not an error.
  - `review_queue.issue_snippet(title: str, body: str | None, max_chars: int = 280) -> str` — `title` alone if `body` is falsy; otherwise `f"{title}\n\n{body}"` with `body` truncated to `max_chars` (ellipsis `…` appended) if longer.
- **Consumes:** nothing from other new modules. Reads raw JSONL files directly (no dependency on `proposals.py` or `decisions.py`) — this is intentional, both to keep the module dependency-free and because the queue only needs `id`/`action`/`confidence`/`proposal_id` shape, not the full proposal/decision schemas.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_review_queue.py
import json

from triage_verse import review_queue


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_load_undecided_sorts_by_confidence_ascending(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {"id": "a", "repo": "r/r", "issue": 1, "action": "add-label", "confidence": 0.9},
            {"id": "b", "repo": "r/r", "issue": 2, "action": "add-label", "confidence": 0.3},
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir)
    assert [r["id"] for r in rows] == ["b", "a"]


def test_load_undecided_excludes_any_verdict(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {"id": "a", "repo": "r/r", "issue": 1, "action": "add-label", "confidence": 0.9},
            {"id": "b", "repo": "r/r", "issue": 2, "action": "add-label", "confidence": 0.3},
        ],
    )
    _write_jsonl(
        decisions_dir / "2026" / "W27.jsonl",
        [{"id": "d1", "proposal_id": "a", "verdict": "skipped"}],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir)
    assert [r["id"] for r in rows] == ["b"]


def test_load_undecided_skips_malformed_lines(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    path = proposals_dir / "2026" / "W27.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id": "a", "repo": "r/r", "issue": 1, "action": "add-label", "confidence": 0.5}\n'
        "not json\n",
        encoding="utf-8",
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir)
    assert [r["id"] for r in rows] == ["a"]


def test_load_undecided_missing_dirs_returns_empty(tmp_path):
    rows = review_queue.load_undecided(tmp_path / "nope-p", tmp_path / "nope-d")
    assert rows == []


def test_load_undecided_excludes_out_of_scope_actions(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {"id": "a", "repo": "r/r", "issue": 1, "action": "add-label", "confidence": 0.5},
            {"id": "b", "repo": "r/r", "issue": 2, "action": "close", "confidence": 0.5},
            {"id": "c", "repo": "r/r", "issue": 3, "action": "close-duplicate", "confidence": 0.5},
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir)
    assert [r["id"] for r in rows] == ["a"]


def test_issue_snippet_truncates_long_body():
    snippet = review_queue.issue_snippet("Title", "x" * 300, max_chars=280)
    assert snippet.startswith("Title\n\n")
    assert snippet.endswith("…")
    assert len(snippet) < len("Title\n\n") + 300


def test_issue_snippet_handles_missing_body():
    assert review_queue.issue_snippet("Title", None) == "Title"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'triage_verse.review_queue'`

- [ ] **Step 3: Write the implementation**

```python
# src/triage_verse/review_queue.py
"""Load undecided review-queue proposals, sorted by confidence."""

from __future__ import annotations

import json
import logging
import pathlib

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = frozenset({"add-label", "set-priority"})


def _iter_jsonl_records(base_dir: str | pathlib.Path) -> list[dict]:
    base = pathlib.Path(base_dir)
    if not base.exists():
        return []
    records: list[dict] = []
    for path in sorted(base.glob("**/*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping malformed JSON line %s:%d", path, lineno)
    return records


def load_undecided(
    proposals_dir: str | pathlib.Path, decisions_dir: str | pathlib.Path
) -> list[dict]:
    decided_ids = {
        r["proposal_id"] for r in _iter_jsonl_records(decisions_dir) if "proposal_id" in r
    }
    proposals = [
        r
        for r in _iter_jsonl_records(proposals_dir)
        if r.get("id") not in decided_ids and r.get("action") in SUPPORTED_ACTIONS
    ]
    return sorted(proposals, key=lambda r: r.get("confidence", 0.0))


def issue_snippet(title: str, body: str | None, max_chars: int = 280) -> str:
    body = (body or "").strip()
    if not body:
        return title
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return f"{title}\n\n{body}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run full checks and commit**

Run: `make py-check`

```bash
git add src/triage_verse/review_queue.py tests/triage_verse/test_review_queue.py
git commit -m "feat: add review_queue module for loading undecided proposals"
```

---

### Task 4: `decisions.py` — record and log decisions

**Files:**
- Create: `src/triage_verse/decisions.py`
- Test: `tests/triage_verse/test_decisions.py`

**Interfaces:**
- Consumes: `jsonl_log.append_weekly(records, base_dir, *, today=None) -> pathlib.Path` (Task 1).
- Produces:
  - `decisions.record(proposal: dict, verdict: str) -> dict` — builds `{id, proposal_id, repo, issue, action, params, verdict, confidence, decided_at}` from a proposal dict (as loaded by `review_queue.load_undecided`) and a verdict string. `id` is a fresh `uuid.uuid4().hex`; `decided_at` is the current UTC time as `YYYY-MM-DDTHH:MM:SSZ`.
  - `decisions.write(records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None) -> pathlib.Path` — thin wrapper over `jsonl_log.append_weekly`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/triage_verse/test_decisions.py
import json

from triage_verse import decisions


def _proposal(**overrides):
    row = {
        "id": "p1",
        "repo": "r/r",
        "issue": 1,
        "action": "add-label",
        "params": {"label": "bug"},
        "confidence": 0.42,
    }
    row.update(overrides)
    return row


def test_record_copies_proposal_fields():
    rec = decisions.record(_proposal(), "approved")
    assert rec["proposal_id"] == "p1"
    assert rec["repo"] == "r/r"
    assert rec["issue"] == 1
    assert rec["action"] == "add-label"
    assert rec["params"] == {"label": "bug"}
    assert rec["verdict"] == "approved"
    assert rec["confidence"] == 0.42
    assert rec["id"] != "p1"
    assert rec["decided_at"].endswith("Z")


def test_write_appends_weekly_partition(tmp_path):
    rec = decisions.record(_proposal(), "rejected")
    path = decisions.write([rec], tmp_path / "decisions", today="2026-06-29")
    assert path.exists()
    assert "2026/W27.jsonl" in str(path).replace("\\", "/")
    line = json.loads(path.read_text().splitlines()[0])
    assert line["verdict"] == "rejected"
    # appends, not overwrites
    decisions.write([rec], tmp_path / "decisions", today="2026-06-29")
    assert len(path.read_text().splitlines()) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_decisions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'triage_verse.decisions'`

- [ ] **Step 3: Write the implementation**

```python
# src/triage_verse/decisions.py
"""Record human review decisions on proposals into a JSONL log."""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timezone

from . import jsonl_log


def record(proposal: dict, verdict: str) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "proposal_id": proposal["id"],
        "repo": proposal["repo"],
        "issue": proposal["issue"],
        "action": proposal["action"],
        "params": proposal["params"],
        "verdict": verdict,
        "confidence": proposal.get("confidence"),
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    return jsonl_log.append_weekly(records, base_dir, today=today)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_decisions.py -v`
Expected: PASS

- [ ] **Step 5: Run full checks and commit**

Run: `make py-check`

```bash
git add src/triage_verse/decisions.py tests/triage_verse/test_decisions.py
git commit -m "feat: add decisions module for recording review verdicts"
```

---

### Task 5: Add the `shiny` dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock` (regenerated automatically)

**Interfaces:** none (dependency-only task).

- [ ] **Step 1: Add the dependency**

Run: `uv add shiny`
Expected: command succeeds; `pyproject.toml`'s `dependencies` array now includes a `shiny` entry (alongside `anthropic`, `fastembed`, `jsonschema`, `pyyaml`, `sqlite-vec`, `zstandard`), and `uv.lock` is updated.

- [ ] **Step 2: Verify it imports**

Run: `uv run python -c "import shiny; print(shiny.__version__)"`
Expected: prints a version string, no error.

- [ ] **Step 3: Run full checks and commit**

Run: `make py-check`
Expected: still green (no source changes, just a new dependency).

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add shiny dependency for the review app"
```

---

### Task 6: Build the review app

**Files:**
- Create: `src/triage_verse/review_app/__init__.py` (empty)
- Create: `src/triage_verse/review_app/app.py`

**Interfaces:**
- Consumes:
  - `db.connect(path) -> sqlite3.Connection`, `db.get_issue(con, repo, number) -> sqlite3.Row | None` (Task 2)
  - `review_queue.load_undecided(proposals_dir, decisions_dir) -> list[dict]`, `review_queue.issue_snippet(title, body, max_chars=280) -> str` (Task 3)
  - `decisions.record(proposal, verdict) -> dict`, `decisions.write(records, base_dir, *, today=None) -> pathlib.Path` (Task 4)
- Produces: a runnable Shiny app object `app` at module level, per Shiny-for-Python convention (`shiny run <path>` looks for a module-level `app`).

This task has no automated tests (per the spec — `app.py` is a thin wire-up over already-tested modules). Each step below ends in a manual check instead of a pytest run.

- [ ] **Step 1: Create the package and empty `__init__.py`**

```bash
mkdir -p src/triage_verse/review_app
touch src/triage_verse/review_app/__init__.py
```

- [ ] **Step 2: Write `app.py`**

```python
# src/triage_verse/review_app/app.py
"""Standalone Shiny app: human review queue for triage-verse proposals.

Run with: shiny run src/triage_verse/review_app/app.py
"""

from __future__ import annotations

import os
import pathlib
from typing import Callable

from shiny import App, Inputs, Outputs, Session, module, reactive, render, ui

from .. import db, decisions, review_queue

DB_PATH = os.environ.get("TRIAGE_VERSE_DB", ".data/mirror.sqlite")
PROPOSALS_DIR = os.environ.get("TRIAGE_VERSE_PROPOSALS", ".data/proposals")
DECISIONS_DIR = os.environ.get("TRIAGE_VERSE_DECISIONS", ".data/decisions")

pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
_con = db.connect(DB_PATH)


def _row_label(proposal: dict) -> str:
    return f"{proposal['repo']}#{proposal['issue']} — {proposal['action']}: {proposal['params']}"


def _row_snippet(proposal: dict) -> str:
    issue = db.get_issue(_con, proposal["repo"], proposal["issue"])
    if issue is None:
        return "(not found in mirror)"
    return review_queue.issue_snippet(issue["title"], issue["body"])


@module.ui
def row_ui(proposal: dict, snippet: str):
    github_url = f"https://github.com/{proposal['repo']}/issues/{proposal['issue']}"
    return ui.card(
        ui.card_header(_row_label(proposal)),
        ui.p(f"confidence: {proposal['confidence']:.2f}"),
        ui.p(proposal.get("rationale") or ""),
        ui.pre(snippet),
        ui.a("View on GitHub", href=github_url, target="_blank"),
        ui.input_action_button("approve", "Approve"),
        ui.input_action_button("reject", "Reject"),
        ui.input_action_button("skip", "Skip"),
    )


@module.server
def row_server(
    input: Inputs,
    output: Outputs,
    session: Session,
    proposal: dict,
    on_decide: Callable[[dict, str], None],
):
    @reactive.effect
    @reactive.event(input.approve)
    def _approve():
        on_decide(proposal, "approved")

    @reactive.effect
    @reactive.event(input.reject)
    def _reject():
        on_decide(proposal, "rejected")

    @reactive.effect
    @reactive.event(input.skip)
    def _skip():
        on_decide(proposal, "skipped")


app_ui = ui.page_fluid(
    ui.h2("Triage review queue"),
    ui.input_action_button("approve_visible", "Approve visible rows"),
    ui.output_ui("queue_ui"),
)


def server(input: Inputs, output: Outputs, session: Session):
    queue = reactive.value(review_queue.load_undecided(PROPOSALS_DIR, DECISIONS_DIR))
    wired: set[str] = set()

    def refresh() -> None:
        queue.set(review_queue.load_undecided(PROPOSALS_DIR, DECISIONS_DIR))

    def on_decide(proposal: dict, verdict: str) -> None:
        decisions.write([decisions.record(proposal, verdict)], DECISIONS_DIR)
        refresh()

    @render.ui
    def queue_ui():
        rows = queue.get()
        if not rows:
            return ui.p("Queue empty — nothing to review.")
        cards = []
        for proposal in rows:
            row_id = proposal["id"]
            if row_id not in wired:
                row_server(row_id, proposal=proposal, on_decide=on_decide)
                wired.add(row_id)
            cards.append(row_ui(row_id, proposal, _row_snippet(proposal)))
        return ui.div(*cards)

    @reactive.effect
    @reactive.event(input.approve_visible)
    def _approve_visible():
        for proposal in queue.get():
            decisions.write([decisions.record(proposal, "approved")], DECISIONS_DIR)
        refresh()


app = App(app_ui, server)
```

- [ ] **Step 3: Type-check and lint**

Run: `make py-check-format py-check-types`
Expected: both pass. If pyright flags the `module.ui`/`module.server` decorated functions (their exact stub types can be finicky across `shiny` versions), fix the reported issue directly rather than suppressing it — these decorators are a documented, standard `shiny` pattern (see `shiny.posit.co/py/docs/module-communication.html`, "Passing callbacks" example), not something to work around.

- [ ] **Step 4: Manual smoke test — empty queue**

Run: `TRIAGE_VERSE_PROPOSALS=/tmp/rq-smoke/proposals TRIAGE_VERSE_DECISIONS=/tmp/rq-smoke/decisions TRIAGE_VERSE_DB=/tmp/rq-smoke/mirror.sqlite uv run shiny run src/triage_verse/review_app/app.py`

Open the printed local URL in a browser. Expected: page loads, header "Triage review queue" visible, body shows "Queue empty — nothing to review." (none of those three dirs/files exist yet). Stop the server (Ctrl-C).

- [ ] **Step 5: Seed fixture data**

```bash
mkdir -p /tmp/rq-smoke/proposals/2026
cat > /tmp/rq-smoke/proposals/2026/W27.jsonl <<'EOF'
{"id": "p1", "repo": "rstudio/reactlog", "issue": 1, "action": "add-label", "params": {"label": "bug"}, "rationale": "looks like a bug report", "confidence": 0.4, "evidence": [], "issue_updated_at": "2026-01-01T00:00:00Z", "run_id": "run1", "model": "claude-haiku-4-5"}
{"id": "p2", "repo": "rstudio/reactlog", "issue": 2, "action": "set-priority", "params": {"priority": "High"}, "rationale": "crashes on load", "confidence": 0.9, "evidence": [], "issue_updated_at": "2026-01-01T00:00:00Z", "run_id": "run1", "model": "claude-haiku-4-5"}
EOF
uv run python - <<'EOF'
from triage_verse import db
con = db.connect("/tmp/rq-smoke/mirror.sqlite")
db.upsert_issue(con, {
    "repo": "rstudio/reactlog", "number": 1, "title": "Crash on startup",
    "body": "Reactlog crashes immediately when the app starts.",
    "state": "OPEN", "state_reason": None, "author": "someone",
    "labels_json": "[]", "assignees_json": "[]", "milestone": None,
    "comment_count": 0, "reaction_count": 0, "is_pr": 0,
    "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    "closed_at": None,
})
con.commit()
EOF
```

(Issue #2 is intentionally left out of the mirror to exercise the "not found in mirror" placeholder.)

- [ ] **Step 6: Manual smoke test — full flow**

Run: `TRIAGE_VERSE_PROPOSALS=/tmp/rq-smoke/proposals TRIAGE_VERSE_DECISIONS=/tmp/rq-smoke/decisions TRIAGE_VERSE_DB=/tmp/rq-smoke/mirror.sqlite uv run shiny run src/triage_verse/review_app/app.py`

In the browser:
1. Confirm two rows render, `p1` (confidence 0.4) above `p2` (confidence 0.9) — ascending order.
2. Confirm `p1`'s card shows the "Crash on startup" title/body snippet and a working "View on GitHub" link to `https://github.com/rstudio/reactlog/issues/1`.
3. Confirm `p2`'s card shows "(not found in mirror)" instead of a snippet.
4. Click **Reject** on `p1`. Confirm its card disappears and only `p2` remains.
5. Click **Approve visible rows**. Confirm `p2` disappears and the page shows "Queue empty — nothing to review."
6. Stop the server. Run `cat /tmp/rq-smoke/decisions/2026/W27.jsonl` and confirm two lines: one `"verdict": "rejected"` for `proposal_id: "p1"`, one `"verdict": "approved"` for `proposal_id: "p2"`.
7. Restart the same `shiny run` command from Step 6. Confirm the queue is still empty (decisions persisted across restart, per the spec's Definition of Done).
8. Clean up: `rm -rf /tmp/rq-smoke`.

- [ ] **Step 7: Commit**

```bash
git add src/triage_verse/review_app/
git commit -m "feat: add review queue Shiny app"
```

---

## Definition of Done (from the spec)

1. ✅ Task 6 Step 6 demonstrates `shiny run src/triage_verse/review_app/app.py` showing an ordered, filtered queue with working approve/reject/skip/bulk-approve.
2. ✅ Task 6 Step 6.6–6.7 demonstrate decisions persist in `.../decisions/YYYY/Www.jsonl` and are never shown again after a restart.
3. ✅ Tasks 3 and 4 give `review_queue.py` and `decisions.py` full offline unit coverage with no network, no model calls, no Shiny test harness.
