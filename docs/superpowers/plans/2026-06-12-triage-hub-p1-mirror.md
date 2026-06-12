# Triage Hub P1: Mirror, Snapshots & Analytics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the P1 data plane from the approved spec (`docs/superpowers/specs/2026-06-12-shinyverse-issue-triage-design.md`): incremental GitHub mirror (issues + PRs + comments, open and closed) into SQLite for all configured shinyverse repos, snapshot publishing/bootstrap via GitHub Releases, burndown analytics computation, and a count-reconciliation command — at $0 model spend.

**Architecture:** A Python package (`src/triage_hub/`) drives everything through the `gh` CLI (no new auth surface): GraphQL for issues/PRs ordered by `updatedAt` DESC with timestamp cursors, REST for repo-wide comment listing with `since` cursors. SQLite is the canonical mirror (WAL mode, idempotent upserts); snapshots are `VACUUM INTO` + zstd uploaded to a rolling `mirror-latest` release plus dated restore points. Analytics are pure functions over the mirror (bisect sweep over created/closed timestamps).

**Tech Stack:** Python ≥3.11 via `uv` (`uv_build` backend), stdlib `sqlite3` + `argparse` + `subprocess`, `pyyaml`, `zstandard`, `pytest` (also collects the existing `unittest` files), `gh` CLI, GitHub Actions CI.

**Plan series:** This is Plan 1 of ~5 (mirror → analysis pipeline → review app → executor → steady-state/tiers). Deliberate P1 deviations from the spec, to be picked up later: the `embeddings` table ships with the analysis-pipeline plan (Plan 2); burndown *rendering* ships with the app plan (P1 exports the series as JSON); `cursors.json` export to the `triage-state` branch ships with the steady-state CI plan (cursors live in the `repos` table until then).

**Conventions:** run all commands from the repo root. Python commands are `uv run ...`. Each task ends with a commit; never commit `.data/`.

---

### Task 1: Python project scaffolding + CI

**Files:**
- Create: `pyproject.toml`
- Create: `src/triage_hub/__init__.py`
- Modify: `.gitignore`
- Modify: `.github/workflows/ci.yml:37-59`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "triage-hub"
version = "0.1.0"
description = "Shinyverse issue triage hub: mirror, analytics, and (later) triage pipeline"
requires-python = ">=3.11"
dependencies = [
    "pyyaml>=6.0",
    "zstandard>=0.22",
]

[project.scripts]
triage-hub = "triage_hub.cli:main"

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["uv_build>=0.8,<2"]
build-backend = "uv_build"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

(`uv_build` is uv's native build backend; it expects the `src/triage_hub/` layout this plan uses, with the module name derived from the project name. If `uv sync` reports the installed uv is too old for the pinned range, widen the `requires` pin to match `uv --version`.)

- [ ] **Step 2: Create `src/triage_hub/__init__.py`**

```python
"""Shinyverse issue triage hub."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Append data dir to `.gitignore`**

Append these lines to the existing `.gitignore` (keep current content):

```
.data/
*.sqlite
*.sqlite.zst
```

- [ ] **Step 4: Install and verify the test suite still passes**

Run: `uv sync && uv run pytest tests/ -v`
Expected: PASS — the existing `tests/test_resolve_label_specs.py` unittest class is collected by pytest and passes (pytest collects unittest classes natively; the `tests/*.mjs` files are ignored). The `triage-hub` CLI entry point does not exist yet — that's fine, nothing imports `triage_hub.cli` until Task 8.

- [ ] **Step 5: Update CI to use uv for all Python steps**

In `.github/workflows/ci.yml`, replace the `Set up Python` / `Install Python dependencies` / `Validate YAML` / `Run Python checks` steps (keep checkout, Node setup, and Node checks unchanged) with:

```yaml
      - name: Set up uv
        uses: astral-sh/setup-uv@v7
        with:
          python-version: '3.13'

      - name: Install Python project
        run: uv sync

      - name: Validate YAML
        run: uv run python .github/triage/scripts/validate-yaml.py

      - name: Run Python checks
        run: |
          uv run python -m py_compile \
            .github/triage/scripts/resolve-repositories.py \
            .github/triage/scripts/resolve-label-specs.py \
            .github/triage/scripts/check-engine-guardrails.py
          uv run pytest tests/ -v
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/triage_hub/__init__.py .gitignore .github/workflows/ci.yml
git commit -m "feat: scaffold triage-hub Python package with uv and pytest"
```

---

### Task 2: Repo scope config (`config/repos.yaml` + loader)

**Files:**
- Create: `config/repos.yaml`
- Create: `src/triage_hub/config.py`
- Test: `tests/triage_hub/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_config.py`:

```python
import pathlib

import pytest

from triage_hub.config import Repo, load_repos

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_load_repos_parses_owner_and_name(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n  - posit-dev/py-shiny\n")

    repos = load_repos(cfg)

    assert repos == [Repo("rstudio", "shiny"), Repo("posit-dev", "py-shiny")]
    assert repos[0].full == "rstudio/shiny"


def test_load_repos_rejects_malformed_entry(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - not-a-repo\n")

    with pytest.raises(ValueError, match="not-a-repo"):
        load_repos(cfg)


def test_checked_in_config_is_pilot_trio():
    repos = load_repos(REPO_ROOT / "config" / "repos.yaml")

    fulls = [r.full for r in repos]
    assert len(fulls) == len(set(fulls))
    assert fulls == ["rstudio/reactlog", "rstudio/shinytest2",
                     "posit-dev/py-shinylive"]


def test_checked_in_config_keeps_fleet_ready_to_uncomment():
    text = (REPO_ROOT / "config" / "repos.yaml").read_text(encoding="utf-8")
    assert "# - rstudio/shiny\n" in text
    assert "# - posit-dev/py-shiny\n" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_hub.config'`

- [ ] **Step 3: Create `config/repos.yaml`**

```yaml
# Repo scope for the triage hub (one tenant = one file like this).
# Start small: the active entries below are the mirror pilot. Uncomment the
# remaining repos when we're ready to run on the full shinyverse.
repositories:
  - rstudio/reactlog
  - rstudio/shinytest2
  - posit-dev/py-shinylive
  # - rstudio/bsicons
  # - rstudio/bslib
  # - r-lib/cachem
  # - rstudio/chromote
  # - rstudio/crosstalk
  # - rstudio/DT
  # - rstudio/dygraphs
  # - r-lib/fastmap
  # - rstudio/flexdashboard
  # - rstudio/fontawesome
  # - rstudio/htmltools
  # - ramnathv/htmlwidgets
  # - rstudio/httpuv
  # - r-lib/later
  # - rstudio/leaflet
  # - rstudio/plumber
  # - rstudio/pool
  # - rstudio/promises
  # - rstudio/sass
  # - rstudio/shiny
  # - rstudio/shinycoreci
  # - schloerke/shinyjster
  # - rstudio/shinymeta
  # - rstudio/shinytest
  # - rstudio/shinythemes
  # - rstudio/shinyvalidate
  # - rstudio/thematic
  # - rstudio/webdriver
  # - rstudio/websocket
  # - posit-dev/py-shiny
  # - posit-dev/py-htmltools
  # - posit-dev/py-shinywidgets
  # - posit-dev/shinylive
  # - posit-dev/chatlas
  # - posit-dev/shinychat
  # - posit-dev/querychat
  # - posit-dev/brand-yml
  # - posit-dev/great-tables
```

- [ ] **Step 4: Create `src/triage_hub/config.py`**

```python
"""Load tenant configuration (repo scope)."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.name}"


def load_repos(path: str | pathlib.Path) -> list[Repo]:
    data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    entries = data.get("repositories") or []
    repos: list[Repo] = []
    for entry in entries:
        owner, sep, name = str(entry).partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError(f"invalid repo entry: {entry!r} (expected owner/name)")
        repos.append(Repo(owner, name))
    return repos
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/triage_hub/test_config.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add config/repos.yaml src/triage_hub/config.py tests/triage_hub/test_config.py
git commit -m "feat: add canonical repo scope config and loader"
```

---

### Task 3: SQLite mirror schema + upserts (`db.py`)

**Files:**
- Create: `src/triage_hub/db.py`
- Test: `tests/triage_hub/test_db.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_db.py`:

```python
from triage_hub import db


def _issue_row(**overrides):
    row = {
        "repo": "rstudio/shiny",
        "number": 1,
        "title": "first",
        "body": "body",
        "state": "OPEN",
        "state_reason": None,
        "author": "alice",
        "labels_json": "[]",
        "assignees_json": "[]",
        "milestone": None,
        "comment_count": 0,
        "reaction_count": 0,
        "is_pr": 0,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "closed_at": None,
    }
    row.update(overrides)
    return row


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "m.sqlite"
    con = db.connect(path)
    con.close()
    con = db.connect(path)  # re-running schema must not fail
    assert con.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0


def test_upsert_issue_twice_updates_in_place(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row())
    db.upsert_issue(con, _issue_row(title="renamed", state="CLOSED",
                                    state_reason="COMPLETED",
                                    closed_at="2024-02-01T00:00:00Z"))

    rows = con.execute("SELECT * FROM issues").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "renamed"
    assert rows[0]["state"] == "CLOSED"
    assert rows[0]["state_reason"] == "COMPLETED"


def test_upsert_pr_and_comment(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row(number=7, is_pr=1))
    db.upsert_pr(con, {"repo": "rstudio/shiny", "number": 7, "merged": 1,
                       "merged_at": "2024-03-01T00:00:00Z",
                       "closing_issue_refs_json": "[3]",
                       "head_ref": "fix", "base_ref": "main"})
    db.upsert_comment(con, {"repo": "rstudio/shiny", "issue_number": 1,
                            "comment_id": 42, "author": "bob", "body": "hi",
                            "created_at": "2024-01-03T00:00:00Z",
                            "updated_at": "2024-01-03T00:00:00Z"})
    db.upsert_comment(con, {"repo": "rstudio/shiny", "issue_number": 1,
                            "comment_id": 42, "author": "bob", "body": "edited",
                            "created_at": "2024-01-03T00:00:00Z",
                            "updated_at": "2024-01-04T00:00:00Z"})

    assert con.execute("SELECT merged FROM prs WHERE number=7").fetchone()[0] == 1
    comments = con.execute("SELECT body FROM comments").fetchall()
    assert [c["body"] for c in comments] == ["edited"]


def test_cursors_roundtrip(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert db.get_cursor(con, "rstudio/shiny", "issues") is None
    db.set_cursor(con, "rstudio/shiny", "issues", "2026-06-01T00:00:00Z")
    db.set_cursor(con, "rstudio/shiny", "comments", "2026-06-02T00:00:00Z")
    assert db.get_cursor(con, "rstudio/shiny", "issues") == "2026-06-01T00:00:00Z"
    assert db.get_cursor(con, "rstudio/shiny", "comments") == "2026-06-02T00:00:00Z"


def test_record_run(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    run_id = db.start_run(con, "sync")
    db.finish_run(con, run_id, {"issues": 3})
    row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["kind"] == "sync"
    assert row["finished_at"] is not None
    assert '"issues": 3' in row["summary_json"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_hub.db'`

- [ ] **Step 3: Create `src/triage_hub/db.py`**

```python
"""SQLite mirror: schema, connection, upserts, cursors, run records."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
  repo TEXT PRIMARY KEY,
  issues_cursor TEXT,
  prs_cursor TEXT,
  comments_cursor TEXT
);
CREATE TABLE IF NOT EXISTS issues (
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  state TEXT NOT NULL,
  state_reason TEXT,
  author TEXT,
  labels_json TEXT NOT NULL DEFAULT '[]',
  assignees_json TEXT NOT NULL DEFAULT '[]',
  milestone TEXT,
  comment_count INTEGER NOT NULL DEFAULT 0,
  reaction_count INTEGER NOT NULL DEFAULT 0,
  is_pr INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS prs (
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  merged INTEGER NOT NULL DEFAULT 0,
  merged_at TEXT,
  closing_issue_refs_json TEXT NOT NULL DEFAULT '[]',
  head_ref TEXT,
  base_ref TEXT,
  PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS comments (
  repo TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  comment_id INTEGER NOT NULL,
  author TEXT,
  body TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (repo, comment_id)
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  summary_json TEXT
);
CREATE TABLE IF NOT EXISTS spend (
  run_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  usd REAL NOT NULL DEFAULT 0,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_updated ON issues(repo, updated_at);
CREATE INDEX IF NOT EXISTS idx_comments_issue ON comments(repo, issue_number);
"""

ISSUE_COLUMNS = (
    "repo", "number", "title", "body", "state", "state_reason", "author",
    "labels_json", "assignees_json", "milestone", "comment_count",
    "reaction_count", "is_pr", "created_at", "updated_at", "closed_at",
)
PR_COLUMNS = ("repo", "number", "merged", "merged_at",
              "closing_issue_refs_json", "head_ref", "base_ref")
COMMENT_COLUMNS = ("repo", "issue_number", "comment_id", "author", "body",
                   "created_at", "updated_at")

_CURSOR_KINDS = {"issues": "issues_cursor", "prs": "prs_cursor",
                 "comments": "comments_cursor"}


def connect(path: str | pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def _upsert(con: sqlite3.Connection, table: str, columns: tuple[str, ...],
            key: tuple[str, ...], row: dict) -> None:
    placeholders = ", ".join(":" + c for c in columns)
    updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in key)
    con.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}",
        row,
    )


def upsert_issue(con: sqlite3.Connection, row: dict) -> None:
    _upsert(con, "issues", ISSUE_COLUMNS, ("repo", "number"), row)


def upsert_pr(con: sqlite3.Connection, row: dict) -> None:
    _upsert(con, "prs", PR_COLUMNS, ("repo", "number"), row)


def upsert_comment(con: sqlite3.Connection, row: dict) -> None:
    _upsert(con, "comments", COMMENT_COLUMNS, ("repo", "comment_id"), row)


def get_cursor(con: sqlite3.Connection, repo: str, kind: str) -> str | None:
    column = _CURSOR_KINDS[kind]
    row = con.execute(f"SELECT {column} FROM repos WHERE repo=?", (repo,)).fetchone()
    return row[column] if row else None


def set_cursor(con: sqlite3.Connection, repo: str, kind: str, value: str) -> None:
    column = _CURSOR_KINDS[kind]
    con.execute("INSERT INTO repos (repo) VALUES (?) ON CONFLICT (repo) DO NOTHING",
                (repo,))
    con.execute(f"UPDATE repos SET {column}=? WHERE repo=?", (value, repo))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_run(con: sqlite3.Connection, kind: str) -> str:
    run_id = uuid.uuid4().hex
    con.execute("INSERT INTO runs (run_id, kind, started_at) VALUES (?, ?, ?)",
                (run_id, kind, _now()))
    con.commit()
    return run_id


def finish_run(con: sqlite3.Connection, run_id: str, summary: dict) -> None:
    con.execute("UPDATE runs SET finished_at=?, summary_json=? WHERE run_id=?",
                (_now(), json.dumps(summary), run_id))
    con.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/triage_hub/test_db.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/triage_hub/db.py tests/triage_hub/test_db.py
git commit -m "feat: add SQLite mirror schema with upserts, cursors, run records"
```

---

### Task 4: `gh` CLI wrapper with retry (`gh.py`)

**Files:**
- Create: `src/triage_hub/gh.py`
- Test: `tests/triage_hub/test_gh.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_gh.py`:

```python
import json
import subprocess

import pytest

from triage_hub import gh


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_gh_returns_stdout(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeProc(stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", fake_run)

    out = gh.run_gh(["api", "rate_limit"])

    assert out == '{"ok": true}'
    assert calls == [["gh", "api", "rate_limit"]]


def test_run_gh_retries_on_rate_limit_then_raises(monkeypatch):
    sleeps = []
    attempts = []

    def fake_run(cmd, **kwargs):
        attempts.append(cmd)
        return FakeProc(returncode=1, stderr="HTTP 403: API rate limit exceeded")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(gh.GhError, match="rate limit"):
        gh.run_gh(["api", "x"], retries=3, sleep=sleeps.append)

    assert len(attempts) == 3
    assert sleeps == [30, 60]  # backoff doubles, no sleep after final attempt


def test_run_gh_fails_fast_on_other_errors(monkeypatch):
    attempts = []

    def fake_run(cmd, **kwargs):
        attempts.append(cmd)
        return FakeProc(returncode=1, stderr="HTTP 404: Not Found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(gh.GhError, match="404"):
        gh.run_gh(["api", "missing"], sleep=lambda s: None)

    assert len(attempts) == 1


def test_gh_json_parses(monkeypatch):
    monkeypatch.setattr(gh, "run_gh", lambda args, **kw: '[{"id": 1}]')
    assert gh.gh_json(["api", "things"]) == [{"id": 1}]


def test_gh_graphql_sends_payload_and_unwraps_data(monkeypatch):
    seen = {}

    def fake_run(args, *, input=None, **kw):
        seen["args"] = args
        seen["payload"] = json.loads(input)
        return json.dumps({"data": {"repository": {"name": "shiny"}}})

    monkeypatch.setattr(gh, "run_gh", fake_run)

    data = gh.gh_graphql("query($x: Int!) { n }", {"x": 1})

    assert data == {"repository": {"name": "shiny"}}
    assert seen["args"] == ["api", "graphql", "--input", "-"]
    assert seen["payload"] == {"query": "query($x: Int!) { n }", "variables": {"x": 1}}


def test_gh_graphql_raises_on_errors(monkeypatch):
    monkeypatch.setattr(
        gh, "run_gh",
        lambda args, **kw: json.dumps({"data": None,
                                       "errors": [{"message": "boom"}]}),
    )
    with pytest.raises(gh.GhError, match="boom"):
        gh.gh_graphql("query { n }", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_gh.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_hub.gh'`

- [ ] **Step 3: Create `src/triage_hub/gh.py`**

```python
"""Thin wrapper around the `gh` CLI (auth and HTTP handled by gh)."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Callable

RETRYABLE_MARKERS = ("rate limit", "HTTP 502", "HTTP 503", "HTTP 504", "timeout")


class GhError(RuntimeError):
    pass


def run_gh(args: list[str], *, input: str | None = None, retries: int = 5,
           sleep: Callable[[float], None] = time.sleep) -> str:
    delay = 30.0
    last_error = "gh failed"
    for attempt in range(retries):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True,
                              input=input)
        if proc.returncode == 0:
            return proc.stdout
        last_error = proc.stderr.strip() or f"gh exited {proc.returncode}"
        retryable = any(marker.lower() in last_error.lower()
                        for marker in RETRYABLE_MARKERS)
        if not retryable:
            raise GhError(last_error)
        if attempt < retries - 1:
            sleep(delay)
            delay *= 2
    raise GhError(last_error)


def gh_json(args: list[str], **kwargs: Any) -> Any:
    out = run_gh(args, **kwargs)
    return json.loads(out) if out.strip() else None


def gh_graphql(query: str, variables: dict, **kwargs: Any) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    out = run_gh(["api", "graphql", "--input", "-"], input=payload, **kwargs)
    body = json.loads(out)
    if body.get("errors"):
        raise GhError(json.dumps(body["errors"]))
    return body["data"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/triage_hub/test_gh.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/triage_hub/gh.py tests/triage_hub/test_gh.py
git commit -m "feat: add gh CLI wrapper with rate-limit retry and GraphQL helper"
```

---

### Task 5: Issue sync (GraphQL, updatedAt-cursor incremental)

**Files:**
- Create: `src/triage_hub/sync.py`
- Test: `tests/triage_hub/test_sync_issues.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_sync_issues.py`:

```python
import json

from triage_hub import db
from triage_hub.sync import parse_issue_node, sync_issues


def _node(number, updated, state="OPEN", **over):
    node = {
        "number": number,
        "title": f"issue {number}",
        "body": "text",
        "state": state,
        "stateReason": "NOT_PLANNED" if state == "CLOSED" else None,
        "author": {"login": "alice"},
        "labels": {"nodes": [{"name": "bug"}]},
        "assignees": {"nodes": []},
        "milestone": None,
        "comments": {"totalCount": 2},
        "reactions": {"totalCount": 5},
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": updated,
        "closedAt": "2024-06-01T00:00:00Z" if state == "CLOSED" else None,
    }
    node.update(over)
    return node


def _page(nodes, has_next=False, end_cursor=None):
    return {"repository": {"issues": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
        "nodes": nodes,
    }}}


def test_parse_issue_node_maps_fields():
    row = parse_issue_node("rstudio/shiny", _node(5, "2026-01-02T00:00:00Z"))

    assert row["repo"] == "rstudio/shiny"
    assert row["number"] == 5
    assert row["state"] == "OPEN"
    assert row["author"] == "alice"
    assert json.loads(row["labels_json"]) == ["bug"]
    assert row["comment_count"] == 2
    assert row["reaction_count"] == 5
    assert row["is_pr"] == 0


def test_parse_issue_node_handles_deleted_author():
    row = parse_issue_node("r/r", _node(1, "2026-01-01T00:00:00Z", author=None))
    assert row["author"] is None


def test_full_sync_walks_all_pages_and_sets_cursor(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    pages = [
        _page([_node(3, "2026-06-03T00:00:00Z"),
               _node(2, "2026-06-02T00:00:00Z")], has_next=True, end_cursor="c1"),
        _page([_node(1, "2026-06-01T00:00:00Z")]),
    ]
    calls = []

    def fake_graphql(query, variables):
        calls.append(variables)
        return pages[len(calls) - 1]

    count = sync_issues(con, "rstudio/shiny", graphql=fake_graphql, full=True)

    assert count == 3
    assert con.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 3
    assert db.get_cursor(con, "rstudio/shiny", "issues") == "2026-06-03T00:00:00Z"
    assert calls[1]["after"] == "c1"


def test_incremental_sync_stops_at_cursor(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.set_cursor(con, "rstudio/shiny", "issues", "2026-06-02T00:00:00Z")
    pages = [
        _page([_node(3, "2026-06-03T00:00:00Z"),
               _node(2, "2026-06-02T00:00:00Z"),   # == cursor: still upserted
               _node(1, "2026-06-01T00:00:00Z")],  # < cursor: stop, not upserted
              has_next=True, end_cursor="c1"),
    ]

    count = sync_issues(con, "rstudio/shiny",
                        graphql=lambda q, v: pages[0], full=False)

    assert count == 2
    numbers = {r["number"] for r in con.execute("SELECT number FROM issues")}
    assert numbers == {2, 3}
    assert db.get_cursor(con, "rstudio/shiny", "issues") == "2026-06-03T00:00:00Z"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_sync_issues.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_hub.sync'`

- [ ] **Step 3: Create `src/triage_hub/sync.py`** (issues portion)

```python
"""Incremental GitHub → SQLite sync.

Issues and PRs walk GraphQL connections ordered by updatedAt DESC and stop at
the stored cursor (a timestamp). GitHub bumps an issue's updatedAt on every new
comment, so commenting on an old issue re-enters it into the sync window.
Upserts are idempotent; re-processing rows at the cursor boundary is harmless.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from . import db
from .gh import gh_graphql

ISSUES_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 50, orderBy: {field: UPDATED_AT, direction: DESC}, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state stateReason
        author { login }
        labels(first: 50) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        milestone { title }
        comments { totalCount }
        reactions { totalCount }
        createdAt updatedAt closedAt
      }
    }
  }
}
"""


def parse_issue_node(repo: str, node: dict) -> dict:
    author = node.get("author") or {}
    milestone = node.get("milestone") or {}
    return {
        "repo": repo,
        "number": node["number"],
        "title": node["title"],
        "body": node.get("body"),
        "state": node["state"],
        "state_reason": node.get("stateReason"),
        "author": author.get("login"),
        "labels_json": json.dumps(
            [l["name"] for l in node["labels"]["nodes"]]),
        "assignees_json": json.dumps(
            [a["login"] for a in node["assignees"]["nodes"]]),
        "milestone": milestone.get("title"),
        "comment_count": node["comments"]["totalCount"],
        "reaction_count": node["reactions"]["totalCount"],
        "is_pr": 0,
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node.get("closedAt"),
    }


def _walk_updated_desc(con: sqlite3.Connection, repo: str, kind: str,
                       query: str, connection_key: str,
                       upsert: Callable[[sqlite3.Connection, dict], int],
                       graphql: Callable, full: bool) -> int:
    owner, name = repo.split("/")
    cursor = None if full else db.get_cursor(con, repo, kind)
    after = None
    newest = cursor
    count = 0
    while True:
        data = graphql(query, {"owner": owner, "name": name, "after": after})
        conn = data["repository"][connection_key]
        stop = False
        for node in conn["nodes"]:
            if cursor is not None and node["updatedAt"] < cursor:
                stop = True
                break
            count += upsert(con, node)
            if newest is None or node["updatedAt"] > newest:
                newest = node["updatedAt"]
        if stop or not conn["pageInfo"]["hasNextPage"]:
            break
        after = conn["pageInfo"]["endCursor"]
    if newest is not None:
        db.set_cursor(con, repo, kind, newest)
    con.commit()
    return count


def sync_issues(con: sqlite3.Connection, repo: str, *,
                graphql: Callable = gh_graphql, full: bool = False) -> int:
    def upsert(con_: sqlite3.Connection, node: dict) -> int:
        db.upsert_issue(con_, parse_issue_node(repo, node))
        return 1

    return _walk_updated_desc(con, repo, "issues", ISSUES_QUERY, "issues",
                              upsert, graphql, full)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/triage_hub/test_sync_issues.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/triage_hub/sync.py tests/triage_hub/test_sync_issues.py
git commit -m "feat: add incremental issue sync with updatedAt cursor"
```

---

### Task 6: PR sync (GraphQL)

**Files:**
- Modify: `src/triage_hub/sync.py` (append)
- Test: `tests/triage_hub/test_sync_prs.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_sync_prs.py`:

```python
import json

from triage_hub import db
from triage_hub.sync import parse_pr_node, sync_prs


def _pr_node(number, updated, **over):
    node = {
        "number": number,
        "title": f"pr {number}",
        "body": "fix",
        "state": "MERGED",
        "author": {"login": "carol"},
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "milestone": None,
        "comments": {"totalCount": 1},
        "createdAt": "2024-05-01T00:00:00Z",
        "updatedAt": updated,
        "closedAt": "2024-05-02T00:00:00Z",
        "merged": True,
        "mergedAt": "2024-05-02T00:00:00Z",
        "headRefName": "fix-thing",
        "baseRefName": "main",
        "closingIssuesReferences": {"nodes": [{"number": 9}]},
    }
    node.update(over)
    return node


def test_parse_pr_node_maps_pr_fields():
    issue_row, pr_row = parse_pr_node("rstudio/shiny",
                                      _pr_node(7, "2026-06-01T00:00:00Z"))

    assert issue_row["is_pr"] == 1
    assert issue_row["state"] == "MERGED"
    assert issue_row["reaction_count"] == 0
    assert pr_row["merged"] == 1
    assert json.loads(pr_row["closing_issue_refs_json"]) == [9]
    assert pr_row["head_ref"] == "fix-thing"


def test_sync_prs_upserts_both_tables(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    page = {"repository": {"pullRequests": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [_pr_node(7, "2026-06-01T00:00:00Z")],
    }}}

    count = sync_prs(con, "rstudio/shiny", graphql=lambda q, v: page, full=True)

    assert count == 1
    assert con.execute(
        "SELECT COUNT(*) FROM issues WHERE is_pr=1").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM prs").fetchone()[0] == 1
    assert db.get_cursor(con, "rstudio/shiny", "prs") == "2026-06-01T00:00:00Z"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_sync_prs.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_pr_node'`

- [ ] **Step 3: Append to `src/triage_hub/sync.py`**

```python
PRS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, orderBy: {field: UPDATED_AT, direction: DESC}, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title body state
        author { login }
        labels(first: 50) { nodes { name } }
        assignees(first: 10) { nodes { login } }
        milestone { title }
        comments { totalCount }
        createdAt updatedAt closedAt
        merged mergedAt headRefName baseRefName
        closingIssuesReferences(first: 10) { nodes { number } }
      }
    }
  }
}
"""


def parse_pr_node(repo: str, node: dict) -> tuple[dict, dict]:
    author = node.get("author") or {}
    milestone = node.get("milestone") or {}
    issue_row = {
        "repo": repo,
        "number": node["number"],
        "title": node["title"],
        "body": node.get("body"),
        "state": node["state"],
        "state_reason": None,
        "author": author.get("login"),
        "labels_json": json.dumps(
            [l["name"] for l in node["labels"]["nodes"]]),
        "assignees_json": json.dumps(
            [a["login"] for a in node["assignees"]["nodes"]]),
        "milestone": milestone.get("title"),
        "comment_count": node["comments"]["totalCount"],
        "reaction_count": 0,
        "is_pr": 1,
        "created_at": node["createdAt"],
        "updated_at": node["updatedAt"],
        "closed_at": node.get("closedAt"),
    }
    pr_row = {
        "repo": repo,
        "number": node["number"],
        "merged": 1 if node.get("merged") else 0,
        "merged_at": node.get("mergedAt"),
        "closing_issue_refs_json": json.dumps(
            [n["number"] for n in node["closingIssuesReferences"]["nodes"]]),
        "head_ref": node.get("headRefName"),
        "base_ref": node.get("baseRefName"),
    }
    return issue_row, pr_row


def sync_prs(con: sqlite3.Connection, repo: str, *,
             graphql: Callable = gh_graphql, full: bool = False) -> int:
    def upsert(con_: sqlite3.Connection, node: dict) -> int:
        issue_row, pr_row = parse_pr_node(repo, node)
        db.upsert_issue(con_, issue_row)
        db.upsert_pr(con_, pr_row)
        return 1

    return _walk_updated_desc(con, repo, "prs", PRS_QUERY, "pullRequests",
                              upsert, graphql, full)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/triage_hub/test_sync_prs.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/triage_hub/sync.py tests/triage_hub/test_sync_prs.py
git commit -m "feat: add PR sync with closing-issue references"
```

---

### Task 7: Comment sync (REST, since-cursor) + comment-recapture test

**Files:**
- Modify: `src/triage_hub/sync.py` (append)
- Test: `tests/triage_hub/test_sync_comments.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_sync_comments.py`. The last test is the spec-mandated comment-recapture test (spec "Testing" section): a comment added to an issue created long before the cursor still lands in the mirror.

```python
from triage_hub import db
from triage_hub.sync import parse_comment, sync_comments


def _item(comment_id, issue_number, updated, body="hello"):
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": "dana"},
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": updated,
        "issue_url": f"https://api.github.com/repos/rstudio/shiny/issues/{issue_number}",
    }


def test_parse_comment_extracts_issue_number():
    row = parse_comment("rstudio/shiny", _item(11, 123, "2026-06-01T00:00:00Z"))
    assert row["issue_number"] == 123
    assert row["comment_id"] == 11
    assert row["author"] == "dana"


def test_sync_comments_pages_until_short_page(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    page1 = [_item(i, 1, f"2026-06-01T00:00:{i:02d}Z") for i in range(100)]
    page2 = [_item(100, 2, "2026-06-02T00:00:00Z")]
    calls = []

    def fake_api(args):
        calls.append(args[0])
        return page1 if "page=1" in args[0] else page2

    count = sync_comments(con, "rstudio/shiny", api=fake_api, full=True)

    assert count == 101
    assert len(calls) == 2
    assert "sort=updated" in calls[0] and "direction=asc" in calls[0]
    assert db.get_cursor(con, "rstudio/shiny", "comments") == "2026-06-02T00:00:00Z"


def test_incremental_sync_passes_since(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.set_cursor(con, "rstudio/shiny", "comments", "2026-06-05T00:00:00Z")
    seen = []

    def fake_api(args):
        seen.append(args[0])
        return []

    sync_comments(con, "rstudio/shiny", api=fake_api)

    assert "since=2026-06-05T00:00:00Z" in seen[0]


def test_comment_on_old_issue_is_recaptured(tmp_path):
    """Spec-mandated: issue created long before the cursor gets a new comment."""
    con = db.connect(tmp_path / "m.sqlite")
    con.execute(
        "INSERT INTO issues (repo, number, title, state, created_at, updated_at)"
        " VALUES ('rstudio/shiny', 50, 'ancient', 'OPEN',"
        " '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')")
    db.set_cursor(con, "rstudio/shiny", "comments", "2026-06-01T00:00:00Z")

    new_comment = _item(99, 50, "2026-06-10T00:00:00Z", body="still broken!")
    sync_comments(con, "rstudio/shiny", api=lambda args: [new_comment])

    row = con.execute(
        "SELECT * FROM comments WHERE issue_number=50").fetchone()
    assert row["body"] == "still broken!"
    assert db.get_cursor(con, "rstudio/shiny", "comments") == "2026-06-10T00:00:00Z"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_sync_comments.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_comment'`

- [ ] **Step 3: Append to `src/triage_hub/sync.py`**

```python
def parse_comment(repo: str, item: dict) -> dict:
    user = item.get("user") or {}
    issue_number = int(item["issue_url"].rstrip("/").rsplit("/", 1)[1])
    return {
        "repo": repo,
        "issue_number": issue_number,
        "comment_id": item["id"],
        "author": user.get("login"),
        "body": item.get("body"),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def sync_comments(con: sqlite3.Connection, repo: str, *,
                  api: Callable = None, full: bool = False) -> int:
    """Repo-wide issue-comment listing (covers issue and PR discussion
    threads; PR diff-review comments are out of scope for the mirror)."""
    from .gh import gh_json
    if api is None:
        api = gh_json
    cursor = None if full else db.get_cursor(con, repo, "comments")
    since = cursor or "1970-01-01T00:00:00Z"
    newest = cursor
    count = 0
    page = 1
    while True:
        path = (f"repos/{repo}/issues/comments"
                f"?sort=updated&direction=asc&per_page=100"
                f"&since={since}&page={page}")
        items = api([path]) or []
        for item in items:
            row = parse_comment(repo, item)
            db.upsert_comment(con, row)
            count += 1
            if newest is None or row["updated_at"] > newest:
                newest = row["updated_at"]
        if len(items) < 100:
            break
        page += 1
    if newest is not None:
        db.set_cursor(con, repo, "comments", newest)
    con.commit()
    return count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/triage_hub/test_sync_comments.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/triage_hub/sync.py tests/triage_hub/test_sync_comments.py
git commit -m "feat: add REST comment sync with since cursor and recapture test"
```

---

### Task 8: Sync orchestration + CLI entry point

**Files:**
- Modify: `src/triage_hub/sync.py` (append)
- Create: `src/triage_hub/cli.py`
- Test: `tests/triage_hub/test_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_cli.py`:

```python
from triage_hub import cli, db
from triage_hub import sync as sync_mod


def test_sync_all_records_run_and_counts(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "m.sqlite")
    monkeypatch.setattr(sync_mod, "sync_issues", lambda con, repo, **kw: 2)
    monkeypatch.setattr(sync_mod, "sync_prs", lambda con, repo, **kw: 1)
    monkeypatch.setattr(sync_mod, "sync_comments", lambda con, repo, **kw: 3)

    summary = sync_mod.sync_all(con, ["rstudio/shiny", "rstudio/bslib"],
                                full=False, log=lambda msg: None)

    assert summary == {"repos": 2, "issues": 4, "prs": 2, "comments": 6}
    run = con.execute("SELECT * FROM runs").fetchone()
    assert run["kind"] == "sync"
    assert run["finished_at"] is not None


def test_cli_sync_invokes_sync_all(tmp_path, monkeypatch):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n")
    captured = {}

    def fake_sync_all(con, repos, *, full, log):
        captured["repos"] = repos
        captured["full"] = full
        return {"repos": 1, "issues": 0, "prs": 0, "comments": 0}

    monkeypatch.setattr(sync_mod, "sync_all", fake_sync_all)

    rc = cli.main(["sync", "--db", str(tmp_path / "m.sqlite"),
                   "--config", str(cfg), "--full"])

    assert rc == 0
    assert captured["repos"] == ["rstudio/shiny"]
    assert captured["full"] is True


def test_cli_sync_single_repo_filter(tmp_path, monkeypatch):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n  - rstudio/bslib\n")
    captured = {}

    def fake_sync_all(con, repos, *, full, log):
        captured["repos"] = repos
        return {"repos": 1, "issues": 0, "prs": 0, "comments": 0}

    monkeypatch.setattr(sync_mod, "sync_all", fake_sync_all)

    rc = cli.main(["sync", "--db", str(tmp_path / "m.sqlite"),
                   "--config", str(cfg), "--repo", "rstudio/bslib"])

    assert rc == 0
    assert captured["repos"] == ["rstudio/bslib"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_hub.cli'`

- [ ] **Step 3: Append `sync_all` to `src/triage_hub/sync.py`**

```python
def sync_all(con: sqlite3.Connection, repos: list[str], *,
             full: bool = False, log: Callable[[str], None] = print) -> dict:
    run_id = db.start_run(con, "sync")
    totals = {"repos": 0, "issues": 0, "prs": 0, "comments": 0}
    for repo in repos:
        log(f"syncing {repo} ...")
        totals["issues"] += sync_issues(con, repo, full=full)
        totals["prs"] += sync_prs(con, repo, full=full)
        totals["comments"] += sync_comments(con, repo, full=full)
        totals["repos"] += 1
        log(f"  done {repo}")
    db.finish_run(con, run_id, totals)
    return totals
```

Note: inside `sync_all`, call the module-level names via plain function calls exactly as written above — tests monkeypatch `triage_hub.sync.sync_issues` etc., so do not import them into local variables.

- [ ] **Step 4: Create `src/triage_hub/cli.py`**

```python
"""triage-hub command-line interface."""

from __future__ import annotations

import argparse
import pathlib

from . import config, db
from . import sync as sync_mod

DEFAULT_DB = ".data/mirror.sqlite"
DEFAULT_CONFIG = "config/repos.yaml"


def _open_db(path: str) -> "db.sqlite3.Connection":
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    return db.connect(path)


def _cmd_sync(args: argparse.Namespace) -> int:
    repos = [r.full for r in config.load_repos(args.config)]
    if args.repo:
        if args.repo not in repos:
            print(f"error: {args.repo} is not in {args.config}")
            return 1
        repos = [args.repo]
    con = _open_db(args.db)
    totals = sync_mod.sync_all(con, repos, full=args.full, log=print)
    print(f"synced {totals['repos']} repos: {totals['issues']} issues, "
          f"{totals['prs']} PRs, {totals['comments']} comments")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triage-hub")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="mirror issues/PRs/comments to SQLite")
    p_sync.add_argument("--db", default=DEFAULT_DB)
    p_sync.add_argument("--config", default=DEFAULT_CONFIG)
    p_sync.add_argument("--repo", help="sync only this owner/name")
    p_sync.add_argument("--full", action="store_true",
                        help="ignore cursors and re-walk everything")
    p_sync.set_defaults(func=_cmd_sync)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/triage_hub/test_cli.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Smoke-test against a real small repo (manual, requires `gh auth`)**

Run: `uv run triage-hub sync --repo rstudio/reactlog --full`
Expected output shape (counts will vary):

```
syncing rstudio/reactlog ...
  done rstudio/reactlog
synced 1 repos: ~60 issues, ~50 PRs, ~250 comments
```

Then: `sqlite3 .data/mirror.sqlite "SELECT COUNT(*) FROM issues WHERE state='OPEN' AND is_pr=0"`
Expected: a number close to 25 (compare with the repo's open-issue count on GitHub).

- [ ] **Step 7: Commit**

```bash
git add src/triage_hub/sync.py src/triage_hub/cli.py tests/triage_hub/test_cli.py
git commit -m "feat: add sync orchestration and triage-hub CLI"
```

---

### Task 9: Snapshot publish & bootstrap (GitHub Releases)

**Files:**
- Create: `src/triage_hub/snapshot.py`
- Modify: `src/triage_hub/cli.py`
- Test: `tests/triage_hub/test_snapshot.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_snapshot.py`:

```python
import pathlib

from triage_hub import db, snapshot


def test_compress_roundtrip(tmp_path):
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello " * 1000)
    packed = tmp_path / "a.zst"
    out = tmp_path / "b.txt"

    snapshot.compress(src, packed)
    snapshot.decompress(packed, out)

    assert out.read_bytes() == src.read_bytes()
    assert packed.stat().st_size < src.stat().st_size


def test_vacuum_to_produces_queryable_copy(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    con.execute("INSERT INTO runs (run_id, kind, started_at)"
                " VALUES ('r1', 'sync', '2026-06-12T00:00:00Z')")
    con.commit()
    out = tmp_path / "copy.sqlite"

    snapshot.vacuum_to(tmp_path / "m.sqlite", out)

    copy = db.connect(out)
    assert copy.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_publish_uploads_latest_and_dated(tmp_path):
    db.connect(tmp_path / "m.sqlite").close()
    commands = []

    def fake_gh(args, **kwargs):
        commands.append(args)
        if args[:2] == ["release", "view"]:
            raise snapshot.GhError("release not found")
        if args[:2] == ["release", "list"]:
            return '[{"tagName": "mirror-2026-06-01"}]'
        return ""

    snapshot.publish(tmp_path / "m.sqlite", gh_run=fake_gh, dated=True,
                     today="2026-06-12")

    flat = [" ".join(c) for c in commands]
    assert any(c.startswith("release create mirror-latest") for c in flat)
    assert any(c.startswith("release upload mirror-latest") and "--clobber" in c
               for c in flat)
    assert any(c.startswith("release create mirror-2026-06-12") for c in flat)


def test_publish_prunes_old_dated_releases(tmp_path):
    db.connect(tmp_path / "m.sqlite").close()
    tags = [f"mirror-2026-05-{d:02d}" for d in range(1, 11)]  # 10 dated tags
    commands = []

    def fake_gh(args, **kwargs):
        commands.append(args)
        if args[:2] == ["release", "view"]:
            return ""  # releases exist
        if args[:2] == ["release", "list"]:
            # list runs after the new dated release was created, so include it
            listed = tags + ["mirror-2026-06-12"]
            return snapshot.json.dumps([{"tagName": t} for t in listed])
        return ""

    snapshot.publish(tmp_path / "m.sqlite", gh_run=fake_gh, dated=True,
                     today="2026-06-12", keep=8)

    deletes = [c for c in commands if c[:2] == ["release", "delete"]]
    deleted_tags = {c[2] for c in deletes}
    # 10 existing + 1 new = 11; keep 8 newest -> delete 3 oldest
    assert deleted_tags == {"mirror-2026-05-01", "mirror-2026-05-02",
                            "mirror-2026-05-03"}


def test_bootstrap_refuses_to_overwrite_without_force(tmp_path):
    target = tmp_path / "m.sqlite"
    target.write_bytes(b"existing")

    try:
        snapshot.bootstrap(target, gh_run=lambda *a, **k: "")
        raised = False
    except snapshot.SnapshotError:
        raised = True

    assert raised
    assert target.read_bytes() == b"existing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_snapshot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_hub.snapshot'`

- [ ] **Step 3: Create `src/triage_hub/snapshot.py`**

```python
"""Publish/bootstrap mirror snapshots as GitHub Release assets.

A rolling `mirror-latest` release is refreshed after every successful run;
dated `mirror-YYYY-MM-DD` releases are restore points (keep the newest N).
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
from datetime import date
from typing import Callable

import zstandard

from .gh import GhError, run_gh

ASSET_NAME = "mirror.sqlite.zst"
LATEST_TAG = "mirror-latest"


class SnapshotError(RuntimeError):
    pass


def vacuum_to(db_path: str | pathlib.Path, out_path: str | pathlib.Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("VACUUM INTO ?", (str(out_path),))
    finally:
        con.close()


def compress(src: str | pathlib.Path, dst: str | pathlib.Path) -> None:
    cctx = zstandard.ZstdCompressor(level=9)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        cctx.copy_stream(fin, fout)


def decompress(src: str | pathlib.Path, dst: str | pathlib.Path) -> None:
    dctx = zstandard.ZstdDecompressor()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        dctx.copy_stream(fin, fout)


def _ensure_release(tag: str, gh_run: Callable) -> None:
    try:
        gh_run(["release", "view", tag])
    except GhError:
        gh_run(["release", "create", tag, "--title", tag,
                "--notes", "triage-hub mirror snapshot", "--latest=false"])


def _prune_dated(gh_run: Callable, keep: int) -> None:
    out = gh_run(["release", "list", "--limit", "100", "--json", "tagName"])
    tags = [r["tagName"] for r in json.loads(out)
            if r["tagName"].startswith("mirror-") and r["tagName"] != LATEST_TAG]
    for tag in sorted(tags, reverse=True)[keep:]:
        gh_run(["release", "delete", tag, "--yes", "--cleanup-tag"])


def publish(db_path: str | pathlib.Path, *, gh_run: Callable = run_gh,
            dated: bool = False, today: str | None = None, keep: int = 8) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        plain = pathlib.Path(tmp) / "mirror.sqlite"
        packed = pathlib.Path(tmp) / ASSET_NAME
        vacuum_to(db_path, plain)
        compress(plain, packed)

        _ensure_release(LATEST_TAG, gh_run)
        gh_run(["release", "upload", LATEST_TAG, str(packed), "--clobber"])

        if dated:
            day = today or date.today().isoformat()
            tag = f"mirror-{day}"
            _ensure_release(tag, gh_run)
            gh_run(["release", "upload", tag, str(packed), "--clobber"])
            _prune_dated(gh_run, keep)
            return tag
    return LATEST_TAG


def bootstrap(db_path: str | pathlib.Path, *, gh_run: Callable = run_gh,
              force: bool = False) -> None:
    db_path = pathlib.Path(db_path)
    if db_path.exists() and not force:
        raise SnapshotError(f"{db_path} exists; pass --force to overwrite")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        packed = pathlib.Path(tmp) / ASSET_NAME
        gh_run(["release", "download", LATEST_TAG, "--pattern", ASSET_NAME,
                "--output", str(packed)])
        decompress(packed, db_path)
```

- [ ] **Step 4: Add CLI subcommands**

In `src/triage_hub/cli.py`, add at the top with the other imports:

```python
from . import snapshot as snapshot_mod
```

Add these command functions after `_cmd_sync`:

```python
def _cmd_snapshot_publish(args: argparse.Namespace) -> int:
    tag = snapshot_mod.publish(args.db, dated=args.dated)
    print(f"published snapshot to release {tag} (and {snapshot_mod.LATEST_TAG})")
    return 0


def _cmd_snapshot_bootstrap(args: argparse.Namespace) -> int:
    snapshot_mod.bootstrap(args.db, force=args.force)
    print(f"bootstrapped {args.db} from {snapshot_mod.LATEST_TAG}")
    return 0
```

And register them inside `build_parser()` before `return parser`:

```python
    p_snap = sub.add_parser("snapshot", help="publish or fetch mirror snapshots")
    snap_sub = p_snap.add_subparsers(dest="snapshot_command", required=True)

    p_pub = snap_sub.add_parser("publish")
    p_pub.add_argument("--db", default=DEFAULT_DB)
    p_pub.add_argument("--dated", action="store_true",
                       help="also cut a dated mirror-YYYY-MM-DD restore point")
    p_pub.set_defaults(func=_cmd_snapshot_publish)

    p_boot = snap_sub.add_parser("bootstrap")
    p_boot.add_argument("--db", default=DEFAULT_DB)
    p_boot.add_argument("--force", action="store_true")
    p_boot.set_defaults(func=_cmd_snapshot_bootstrap)
```

- [ ] **Step 5: Run the full suite to verify everything passes**

Run: `uv run pytest tests/ -v`
Expected: PASS (all tests so far)

- [ ] **Step 6: Commit**

```bash
git add src/triage_hub/snapshot.py src/triage_hub/cli.py tests/triage_hub/test_snapshot.py
git commit -m "feat: add snapshot publish/bootstrap via GitHub Releases"
```

---

### Task 10: Analytics (burndown series, weekly flux, close reasons)

**Files:**
- Create: `src/triage_hub/analytics.py`
- Modify: `src/triage_hub/cli.py`
- Test: `tests/triage_hub/test_analytics.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_analytics.py`:

```python
import json

from triage_hub import analytics, db


def _seed(con):
    rows = [
        # (number, created, closed, state, state_reason)
        (1, "2026-01-05T10:00:00Z", None, "OPEN", None),
        (2, "2026-01-06T10:00:00Z", "2026-01-20T10:00:00Z", "CLOSED", "COMPLETED"),
        (3, "2026-01-15T10:00:00Z", "2026-01-21T10:00:00Z", "CLOSED", "NOT_PLANNED"),
        (4, "2026-02-02T10:00:00Z", None, "OPEN", None),
    ]
    for number, created, closed, state, reason in rows:
        con.execute(
            "INSERT INTO issues (repo, number, title, state, state_reason,"
            " created_at, updated_at, closed_at)"
            " VALUES ('rstudio/shiny', ?, 't', ?, ?, ?, ?, ?)",
            (number, state, reason, created, created, closed))
    con.commit()


def test_weekly_open_counts_sweeps_history(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)

    series = analytics.weekly_open_counts(con, as_of="2026-02-09T00:00:00Z")

    by_week = {p["week"]: p["open"] for p in series}
    # Monday 2026-01-12: issues 1 and 2 created, none closed yet -> 2 open
    assert by_week["2026-W03"] == 2
    # Monday 2026-01-26: issues 2 and 3 closed -> only issue 1 open
    assert by_week["2026-W05"] == 1
    # Monday 2026-02-09: issue 4 also open -> 2 open
    assert by_week["2026-W07"] == 2


def test_weekly_flux_counts_opened_and_closed(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)

    flux = analytics.weekly_flux(con)

    by_week = {f["week"]: f for f in flux}
    assert by_week["2026-W02"]["opened"] == 2
    assert by_week["2026-W04"]["closed"] == 2


def test_close_reason_mix(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)

    mix = analytics.close_reason_mix(con)

    assert mix == {"COMPLETED": 1, "NOT_PLANNED": 1}


def test_export_writes_json(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)
    out = tmp_path / "analytics.json"

    analytics.export(con, out)

    data = json.loads(out.read_text())
    assert "generated_at" in data
    assert "rstudio/shiny" in data["repos"]
    repo_block = data["repos"]["rstudio/shiny"]
    assert {"weekly_open", "weekly_flux", "close_reasons"} <= set(repo_block)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_analytics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_hub.analytics'`

- [ ] **Step 3: Create `src/triage_hub/analytics.py`**

```python
"""Burndown and flux analytics over the mirror (issues only, not PRs).

All series are computed from created_at/closed_at, so history is correct
retroactively — including for periods before this project existed.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone


def _iso_week(stamp: str) -> str:
    day = date.fromisoformat(stamp[:10])
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _mondays(first_stamp: str, last_stamp: str) -> list[date]:
    start = date.fromisoformat(first_stamp[:10])
    end = date.fromisoformat(last_stamp[:10])
    monday = start - timedelta(days=start.weekday())
    out = []
    while monday <= end:
        out.append(monday)
        monday += timedelta(days=7)
    return out


def _issue_stamps(con: sqlite3.Connection, repo: str | None):
    where = "WHERE is_pr=0" + (" AND repo=:repo" if repo else "")
    rows = con.execute(
        f"SELECT created_at, closed_at FROM issues {where}",
        {"repo": repo} if repo else {}).fetchall()
    created = sorted(r["created_at"] for r in rows)
    closed = sorted(r["closed_at"] for r in rows if r["closed_at"])
    return created, closed


def weekly_open_counts(con: sqlite3.Connection, *, repo: str | None = None,
                       as_of: str | None = None) -> list[dict]:
    created, closed = _issue_stamps(con, repo)
    if not created:
        return []
    end = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    series = []
    for monday in _mondays(created[0], end):
        boundary = monday.isoformat() + "T00:00:00Z"
        open_count = (bisect_right(created, boundary)
                      - bisect_right(closed, boundary))
        year, week, _ = monday.isocalendar()
        series.append({"week": f"{year}-W{week:02d}", "open": open_count})
    return series


def weekly_flux(con: sqlite3.Connection, *, repo: str | None = None) -> list[dict]:
    created, closed = _issue_stamps(con, repo)
    counts: dict[str, dict] = {}
    for stamp in created:
        week = _iso_week(stamp)
        counts.setdefault(week, {"week": week, "opened": 0, "closed": 0})
        counts[week]["opened"] += 1
    for stamp in closed:
        week = _iso_week(stamp)
        counts.setdefault(week, {"week": week, "opened": 0, "closed": 0})
        counts[week]["closed"] += 1
    return sorted(counts.values(), key=lambda f: f["week"])


def close_reason_mix(con: sqlite3.Connection, *,
                     repo: str | None = None) -> dict[str, int]:
    where = "WHERE is_pr=0 AND state='CLOSED'" + (" AND repo=:repo" if repo else "")
    rows = con.execute(
        f"SELECT COALESCE(state_reason, 'UNSPECIFIED') AS reason,"
        f" COUNT(*) AS n FROM issues {where} GROUP BY reason",
        {"repo": repo} if repo else {}).fetchall()
    return {r["reason"]: r["n"] for r in rows}


def export(con: sqlite3.Connection, out_path: str | pathlib.Path) -> None:
    repos = [r["repo"] for r in
             con.execute("SELECT DISTINCT repo FROM issues ORDER BY repo")]
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "weekly_open": weekly_open_counts(con),
            "weekly_flux": weekly_flux(con),
            "close_reasons": close_reason_mix(con),
        },
        "repos": {
            repo: {
                "weekly_open": weekly_open_counts(con, repo=repo),
                "weekly_flux": weekly_flux(con, repo=repo),
                "close_reasons": close_reason_mix(con, repo=repo),
            }
            for repo in repos
        },
    }
    pathlib.Path(out_path).write_text(json.dumps(payload, indent=2),
                                      encoding="utf-8")
```

- [ ] **Step 4: Add CLI subcommand**

In `src/triage_hub/cli.py`, add to the imports:

```python
from . import analytics as analytics_mod
```

Add the command function:

```python
def _cmd_analytics_export(args: argparse.Namespace) -> int:
    con = _open_db(args.db)
    analytics_mod.export(con, args.out)
    print(f"wrote {args.out}")
    return 0
```

Register inside `build_parser()`:

```python
    p_an = sub.add_parser("analytics", help="compute burndown analytics")
    an_sub = p_an.add_subparsers(dest="analytics_command", required=True)
    p_exp = an_sub.add_parser("export")
    p_exp.add_argument("--db", default=DEFAULT_DB)
    p_exp.add_argument("--out", default=".data/analytics.json")
    p_exp.set_defaults(func=_cmd_analytics_export)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/triage_hub/test_analytics.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add src/triage_hub/analytics.py src/triage_hub/cli.py tests/triage_hub/test_analytics.py
git commit -m "feat: add retroactive burndown analytics with JSON export"
```

---

### Task 11: Count reconciliation (`verify-counts`)

**Files:**
- Create: `src/triage_hub/verify.py`
- Modify: `src/triage_hub/cli.py`
- Test: `tests/triage_hub/test_verify.py`

- [ ] **Step 1: Write the failing test**

Create `tests/triage_hub/test_verify.py`:

```python
from triage_hub import db, verify


def _seed_open(con, repo, n):
    for i in range(n):
        con.execute(
            "INSERT INTO issues (repo, number, title, state, created_at,"
            " updated_at) VALUES (?, ?, 't', 'OPEN',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (repo, i + 1))
    con.commit()


def test_verify_counts_reports_match_and_mismatch(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_open(con, "rstudio/shiny", 10)
    _seed_open(con, "rstudio/bslib", 4)
    github_counts = {"rstudio/shiny": 10, "rstudio/bslib": 9}

    def fake_api(args):
        assert args[0] == "api"
        for repo, total in github_counts.items():
            if f"repo:{repo}" in args[1]:
                return {"total_count": total}
        raise AssertionError(f"unexpected call: {args}")

    results = verify.verify_counts(con, ["rstudio/shiny", "rstudio/bslib"],
                                   api=fake_api, tolerance=2)

    by_repo = {r["repo"]: r for r in results}
    assert by_repo["rstudio/shiny"]["ok"] is True
    assert by_repo["rstudio/bslib"]["ok"] is False
    assert by_repo["rstudio/bslib"]["mirror"] == 4
    assert by_repo["rstudio/bslib"]["github"] == 9


def test_small_drift_within_tolerance_is_ok(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_open(con, "rstudio/shiny", 10)

    results = verify.verify_counts(
        con, ["rstudio/shiny"],
        api=lambda args: {"total_count": 11}, tolerance=2)

    assert results[0]["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_hub/test_verify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'triage_hub.verify'`

- [ ] **Step 3: Create `src/triage_hub/verify.py`**

```python
"""Reconcile mirror open-issue counts against GitHub search totals."""

from __future__ import annotations

import sqlite3
from typing import Callable

from .gh import gh_json


def verify_counts(con: sqlite3.Connection, repos: list[str], *,
                  api: Callable = gh_json, tolerance: int = 2) -> list[dict]:
    results = []
    for repo in repos:
        mirror = con.execute(
            "SELECT COUNT(*) FROM issues"
            " WHERE repo=? AND state='OPEN' AND is_pr=0", (repo,)).fetchone()[0]
        data = api(["api", f"search/issues?q=repo:{repo}+type:issue+state:open"
                    f"&per_page=1"])
        github = data["total_count"]
        results.append({
            "repo": repo,
            "mirror": mirror,
            "github": github,
            "ok": abs(mirror - github) <= tolerance,
        })
    return results
```

- [ ] **Step 4: Add CLI subcommand**

In `src/triage_hub/cli.py`, add to the imports:

```python
from . import verify as verify_mod
```

Add the command function:

```python
def _cmd_verify_counts(args: argparse.Namespace) -> int:
    repos = [r.full for r in config.load_repos(args.config)]
    con = _open_db(args.db)
    results = verify_mod.verify_counts(con, repos)
    bad = [r for r in results if not r["ok"]]
    for r in results:
        flag = "OK " if r["ok"] else "MISMATCH"
        print(f"{flag} {r['repo']}: mirror={r['mirror']} github={r['github']}")
    print(f"{len(results) - len(bad)}/{len(results)} repos reconcile")
    return 1 if bad else 0
```

Register inside `build_parser()`:

```python
    p_ver = sub.add_parser("verify-counts",
                           help="reconcile mirror vs GitHub open-issue counts")
    p_ver.add_argument("--db", default=DEFAULT_DB)
    p_ver.add_argument("--config", default=DEFAULT_CONFIG)
    p_ver.set_defaults(func=_cmd_verify_counts)
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add src/triage_hub/verify.py src/triage_hub/cli.py tests/triage_hub/test_verify.py
git commit -m "feat: add verify-counts reconciliation command"
```

---

### Task 12: Operator runbook + final verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Mirror pipeline (P1)" section to `README.md`**

Append after the existing content:

```markdown
## Mirror pipeline (P1)

The `triage-hub` CLI (Python, managed with [uv](https://docs.astral.sh/uv/))
mirrors issues, PRs, and comments from every repo in `config/repos.yaml` into
a local SQLite database. GitHub stays the source of truth; the mirror is
derived data and can always be rebuilt.

```bash
uv sync                                  # one-time setup
uv run triage-hub sync --full            # initial backfill (resumable)
uv run triage-hub sync                   # incremental refresh (seconds-minutes)
uv run triage-hub verify-counts          # reconcile against GitHub search
uv run triage-hub analytics export       # burndown series -> .data/analytics.json
uv run triage-hub snapshot publish --dated   # upload to mirror-latest + dated tag
uv run triage-hub snapshot bootstrap     # fresh machine: pull mirror-latest
```

Cursors live in the mirror's `repos` table; `--full` ignores them. The
backfill is resumable: re-running `sync --full` re-upserts idempotently, and
interrupted incremental syncs simply continue from the last cursor.

Design: `docs/superpowers/specs/2026-06-12-shinyverse-issue-triage-design.md`.
```

- [ ] **Step 2: Run the complete test suite one final time**

Run: `uv run pytest tests/ -v && node --test tests/test_process_triage_actions.mjs tests/test_gh_token_router.mjs`
Expected: all Python tests PASS; both Node test files PASS (unchanged behavior).

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add P1 mirror pipeline runbook"
```

---

## P1 exit criteria mapping (from the spec)

| Spec exit criterion | How this plan satisfies it |
|---|---|
| Counts reconcile with GitHub search | `triage-hub verify-counts` (Task 11), exit code enforces it |
| Snapshot bootstrap works on a clean machine | `triage-hub snapshot bootstrap` (Task 9) + runbook (Task 12) |
| $0 model spend | No Anthropic calls anywhere in P1; `spend` table exists (Task 3) but stays empty |
| Sync all configured repos, open+closed, comments, PRs | Tasks 5–8 + `config/repos.yaml` (Task 2). Pilot trio is active; the rest of the shinyverse ships commented out, activated by uncommenting when ready |
| Burndown renders | Data + JSON export here (Task 10); rendering lands with the app plan |

**Post-plan operator step (not a code task):** run `uv run triage-hub sync --full` for the real backfill, then `verify-counts`, then `snapshot publish --dated`. The pilot trio backfills in minutes; once the rest of the fleet is uncommented in `config/repos.yaml`, expect a few hours within GraphQL/REST rate budgets. Either way it is resumable if interrupted.
