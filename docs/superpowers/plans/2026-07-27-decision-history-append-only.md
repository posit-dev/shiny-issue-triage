# Append-only Decision History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the `classifications` and `dedup_verdicts` tables from
current-state upserts to append-only history keyed by `run_id`, add `*_latest`
views for current-verdict reads, and migrate existing databases in place.

**Architecture:** Widen each decision table's primary key with `run_id` so each
analyze run writes a new history row (recheck refines that run's row via
`ON CONFLICT`). Add two views returning the newest row per key; point
current-verdict reads at the views. A `PRAGMA user_version`-gated migration in
`db.connect()` rebuilds old-shape tables while preserving rows.

**Tech Stack:** Python 3, SQLite (via stdlib `sqlite3` + `sqlite-vec`), pytest,
managed with `uv`.

## Global Constraints

- Python work is gated by `make py-check` (ruff format + lint, pyright, pytest);
  it must pass before the work is done.
- Run single tests with `uv run pytest <path>::<name>`.
- History granularity is **one row per run**: `store()` keeps `ON CONFLICT … DO
  UPDATE` on the widened key so the `recheck` stage refines the same run's row
  rather than adding a second row.
- GitHub is the source of truth; `.data/mirror.sqlite` is derived. Do not weaken
  the egress guard or touch `gh.py` — this is a local-DB-only change.
- `proposals.build()` intentionally stays on the base tables filtered by
  `run_id`; do **not** repoint it at a `_latest` view.

---

### Task 1: Widen decision-table primary keys and add `*_latest` views

Fresh databases get the new PKs and the two latest views directly. Current-
verdict read helpers (`get_classification`, `get_dedup_verdict`) read from the
views. `upsert_*` write to the widened key. (Migration of *existing* DBs is
Task 2.)

**Files:**
- Modify: `src/triage_verse/db.py` (SCHEMA, `connect`, `upsert_classification`,
  `get_classification`, `upsert_dedup_verdict`, `get_dedup_verdict`)
- Test: `tests/triage_verse/test_db_analysis.py`

**Interfaces:**
- Consumes: `db.connect(path)`, `db._upsert`, existing `CLASSIFICATION_COLUMNS`
  / `DEDUP_COLUMNS` tuples.
- Produces (unchanged signatures, new behavior):
  - `db.upsert_classification(con, row: dict) -> None` — key `(repo, number, run_id)`
  - `db.get_classification(con, repo: str, number: int) -> sqlite3.Row | None` — reads `classifications_latest`
  - `db.upsert_dedup_verdict(con, row: dict) -> None` — key `(repo_a, number_a, repo_b, number_b, run_id)`
  - `db.get_dedup_verdict(con, repo_a, number_a, repo_b, number_b) -> sqlite3.Row | None` — reads `dedup_verdicts_latest`
  - New views `classifications_latest`, `dedup_verdicts_latest`.

- [ ] **Step 1: Write failing tests**

Add to `tests/triage_verse/test_db_analysis.py`:

```python
def test_classification_append_across_runs(tmp_path):
    con = _con(tmp_path)
    base = {
        "repo": "r/r", "number": 1, "clf_hash": "h1", "type": "fix",
        "priority": "High", "assessment": "actionable", "labels_json": "[]",
        "close_candidate_json": None, "confidence": 0.9,
        "model": "claude-haiku-4-5", "run_id": "run1",
        "at": "2026-06-29T00:00:00Z",
    }
    db.upsert_classification(con, base)
    db.upsert_classification(
        con,
        {**base, "clf_hash": "h2", "type": "feat", "confidence": 0.5,
         "model": "claude-sonnet-5", "run_id": "run2",
         "at": "2026-06-29T02:00:00Z"},
    )
    # Both runs retained as history.
    rows = con.execute(
        "SELECT run_id, type FROM classifications "
        "WHERE repo='r/r' AND number=1 ORDER BY at"
    ).fetchall()
    assert [(r["run_id"], r["type"]) for r in rows] == [
        ("run1", "fix"), ("run2", "feat")]
    # Latest view / get_classification return the newest run.
    row = db.get_classification(con, "r/r", 1)
    assert row["run_id"] == "run2" and row["type"] == "feat"


def test_classification_recheck_updates_same_run(tmp_path):
    con = _con(tmp_path)
    base = {
        "repo": "r/r", "number": 1, "clf_hash": "h1", "type": "fix",
        "priority": "High", "assessment": "actionable", "labels_json": "[]",
        "close_candidate_json": None, "confidence": 0.9,
        "model": "claude-haiku-4-5", "run_id": "run1",
        "at": "2026-06-29T00:00:00Z",
    }
    db.upsert_classification(con, base)  # classify
    db.upsert_classification(
        con, {**base, "type": "feat", "confidence": 0.4,
              "at": "2026-06-29T00:05:00Z"})  # recheck, same run_id
    rows = con.execute(
        "SELECT COUNT(*) c FROM classifications "
        "WHERE repo='r/r' AND number=1"
    ).fetchone()
    assert rows["c"] == 1  # one row per run
    assert db.get_classification(con, "r/r", 1)["type"] == "feat"


def test_dedup_append_across_runs(tmp_path):
    con = _con(tmp_path)
    base = {
        "repo_a": "r/a", "number_a": 1, "repo_b": "r/b", "number_b": 2,
        "hash_a": "ha", "hash_b": "hb", "verdict": "duplicate",
        "canonical_json": '"r/a#1"', "cross_repo_option": "close-and-link",
        "confidence": 0.8, "rationale": "same", "model": "claude-sonnet-5",
        "run_id": "run1", "at": "2026-06-29T00:00:00Z",
    }
    db.upsert_dedup_verdict(con, base)
    db.upsert_dedup_verdict(
        con, {**base, "verdict": "not-duplicate", "run_id": "run2",
              "at": "2026-06-29T02:00:00Z"})
    assert con.execute(
        "SELECT COUNT(*) c FROM dedup_verdicts").fetchone()["c"] == 2
    assert db.get_dedup_verdict(con, "r/a", 1, "r/b", 2)["verdict"] == "not-duplicate"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_db_analysis.py -k "append_across_runs or recheck_updates_same_run" -v`
Expected: FAIL — second `upsert_*` overwrites the first row (only one row, or the
`ORDER BY at` history has a single entry).

- [ ] **Step 3: Widen the primary keys in `SCHEMA`**

In `src/triage_verse/db.py`, in the `SCHEMA` string, change the two decision
tables' PK lines:

```sql
CREATE TABLE IF NOT EXISTS classifications (
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  clf_hash TEXT NOT NULL,
  type TEXT NOT NULL,
  priority TEXT NOT NULL,
  assessment TEXT NOT NULL,
  labels_json TEXT NOT NULL DEFAULT '[]',
  close_candidate_json TEXT,
  confidence REAL NOT NULL,
  model TEXT NOT NULL,
  run_id TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (repo, number, run_id)
);
```

```sql
CREATE TABLE IF NOT EXISTS dedup_verdicts (
  repo_a TEXT NOT NULL, number_a INTEGER NOT NULL,
  repo_b TEXT NOT NULL, number_b INTEGER NOT NULL,
  hash_a TEXT NOT NULL, hash_b TEXT NOT NULL,
  verdict TEXT NOT NULL,
  canonical_json TEXT,
  cross_repo_option TEXT,
  confidence REAL NOT NULL,
  rationale TEXT NOT NULL,
  model TEXT NOT NULL,
  run_id TEXT NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (repo_a, number_a, repo_b, number_b, run_id)
);
```

- [ ] **Step 4: Add the latest views**

Add a module-level `SCHEMA_VIEWS` constant in `src/triage_verse/db.py` (place it
right after the `SCHEMA` string):

```python
SCHEMA_VIEWS = """
CREATE VIEW IF NOT EXISTS classifications_latest AS
SELECT c.* FROM classifications c
WHERE c.rowid = (
  SELECT c2.rowid FROM classifications c2
  WHERE c2.repo = c.repo AND c2.number = c.number
  ORDER BY c2.at DESC, c2.rowid DESC
  LIMIT 1
);
CREATE VIEW IF NOT EXISTS dedup_verdicts_latest AS
SELECT d.* FROM dedup_verdicts d
WHERE d.rowid = (
  SELECT d2.rowid FROM dedup_verdicts d2
  WHERE d2.repo_a = d.repo_a AND d2.number_a = d.number_a
    AND d2.repo_b = d.repo_b AND d2.number_b = d.number_b
  ORDER BY d2.at DESC, d2.rowid DESC
  LIMIT 1
);
"""
```

- [ ] **Step 5: Create the views in `connect()`**

In `connect()`, after `con.executescript(SCHEMA)` and before the `vec_issues`
virtual-table creation, add:

```python
    con.executescript(SCHEMA_VIEWS)
```

(Task 2 inserts the migration call between `executescript(SCHEMA)` and this
line; leaving a gap here is fine.)

- [ ] **Step 6: Widen the write keys**

In `src/triage_verse/db.py`, update the two upsert helpers to include `run_id`
in the conflict key:

```python
def upsert_classification(con: sqlite3.Connection, row: dict) -> None:
    _upsert(
        con, "classifications", CLASSIFICATION_COLUMNS,
        ("repo", "number", "run_id"), row,
    )
```

```python
def upsert_dedup_verdict(con: sqlite3.Connection, row: dict) -> None:
    _upsert(
        con,
        "dedup_verdicts",
        DEDUP_COLUMNS,
        ("repo_a", "number_a", "repo_b", "number_b", "run_id"),
        row,
    )
```

- [ ] **Step 7: Point current-verdict reads at the views**

```python
def get_classification(
    con: sqlite3.Connection, repo: str, number: int
) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM classifications_latest WHERE repo=? AND number=?",
        (repo, number),
    ).fetchone()
```

```python
def get_dedup_verdict(
    con: sqlite3.Connection, repo_a: str, number_a: int, repo_b: str, number_b: int
) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM dedup_verdicts_latest "
        "WHERE repo_a=? AND number_a=? AND repo_b=? AND number_b=?",
        (repo_a, number_a, repo_b, number_b),
    ).fetchone()
```

- [ ] **Step 8: Run the new + existing DB tests**

Run: `uv run pytest tests/triage_verse/test_db_analysis.py tests/triage_verse/test_classify.py tests/triage_verse/test_dedup.py -v`
Expected: PASS. Note `test_classification_upsert_roundtrip` still passes — its
two upserts share `run_id="run1"`, so the widened key still updates in place and
`get_classification` returns `h2`.

- [ ] **Step 9: Commit**

```bash
git add src/triage_verse/db.py tests/triage_verse/test_db_analysis.py
git commit -m "feat(db): append-only decision tables keyed by run_id with latest views (#20)"
```

---

### Task 2: Migrate existing databases in place

Rebuild old-shape decision tables (PK without `run_id`) while preserving rows,
gated on `PRAGMA user_version`.

**Files:**
- Modify: `src/triage_verse/db.py` (add `_migrate`, call it from `connect`)
- Test: `tests/triage_verse/test_db_migrate.py` (create)

**Interfaces:**
- Consumes: `SCHEMA`, `CLASSIFICATION_COLUMNS`, `DEDUP_COLUMNS`, `connect`.
- Produces: `db._migrate(con: sqlite3.Connection) -> None` (idempotent; bumps
  `user_version` to 1).

- [ ] **Step 1: Write the failing migration test**

Create `tests/triage_verse/test_db_migrate.py`:

```python
# tests/triage_verse/test_db_migrate.py
import sqlite3

from triage_verse import db


def _old_schema_db(path):
    """A mirror created with the pre-#20 decision-table PKs and one row each."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE classifications (
          repo TEXT NOT NULL, number INTEGER NOT NULL, clf_hash TEXT NOT NULL,
          type TEXT NOT NULL, priority TEXT NOT NULL, assessment TEXT NOT NULL,
          labels_json TEXT NOT NULL DEFAULT '[]', close_candidate_json TEXT,
          confidence REAL NOT NULL, model TEXT NOT NULL, run_id TEXT NOT NULL,
          at TEXT NOT NULL, PRIMARY KEY (repo, number)
        );
        CREATE TABLE dedup_verdicts (
          repo_a TEXT NOT NULL, number_a INTEGER NOT NULL,
          repo_b TEXT NOT NULL, number_b INTEGER NOT NULL,
          hash_a TEXT NOT NULL, hash_b TEXT NOT NULL, verdict TEXT NOT NULL,
          canonical_json TEXT, cross_repo_option TEXT, confidence REAL NOT NULL,
          rationale TEXT NOT NULL, model TEXT NOT NULL, run_id TEXT NOT NULL,
          at TEXT NOT NULL,
          PRIMARY KEY (repo_a, number_a, repo_b, number_b)
        );
        """
    )
    con.execute(
        "INSERT INTO classifications VALUES "
        "('r/r',1,'h1','fix','High','actionable','[]',NULL,0.9,"
        "'claude-haiku-4-5','runOld','2026-06-01T00:00:00Z')"
    )
    con.execute(
        "INSERT INTO dedup_verdicts VALUES "
        "('r/a',1,'r/b',2,'ha','hb','duplicate','\"r/a#1\"','close-and-link',"
        "0.8,'same','claude-sonnet-5','runOld','2026-06-01T00:00:00Z')"
    )
    con.commit()
    con.close()


def _pk_cols(con, table):
    return {r["name"]: r["pk"] for r in con.execute(f"PRAGMA table_info({table})")}


def test_migrate_preserves_rows_and_widens_pk(tmp_path):
    path = tmp_path / "m.sqlite"
    _old_schema_db(path)

    con = db.connect(path)  # runs _migrate

    assert _pk_cols(con, "classifications")["run_id"] > 0
    assert _pk_cols(con, "dedup_verdicts")["run_id"] > 0
    assert db.get_classification(con, "r/r", 1)["run_id"] == "runOld"
    assert db.get_dedup_verdict(con, "r/a", 1, "r/b", 2)["run_id"] == "runOld"
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "m.sqlite"
    _old_schema_db(path)
    db.connect(path).close()
    con = db.connect(path)  # second connect must be a no-op
    assert db.get_classification(con, "r/r", 1)["run_id"] == "runOld"
    assert con.execute(
        "SELECT COUNT(*) FROM classifications").fetchone()[0] == 1


def test_fresh_db_has_new_pk_and_version(tmp_path):
    con = db.connect(tmp_path / "fresh.sqlite")
    assert _pk_cols(con, "classifications")["run_id"] > 0
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_db_migrate.py -v`
Expected: FAIL — `user_version` is 0 and the old tables keep their narrow PK
(`_pk_cols(...)["run_id"] == 0`).

- [ ] **Step 3: Implement `_migrate`**

Add to `src/triage_verse/db.py` (near `connect`):

```python
_SCHEMA_VERSION = 1


def _pk_has_run_id(con: sqlite3.Connection, table: str) -> bool:
    return any(
        r["name"] == "run_id" and r["pk"] > 0
        for r in con.execute(f"PRAGMA table_info({table})")
    )


def _rebuild_with_run_id_pk(
    con: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> None:
    cols = ", ".join(columns)
    con.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
    con.executescript(SCHEMA)  # recreates {table} with the new PK (IF NOT EXISTS)
    con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_old")
    con.execute(f"DROP TABLE {table}_old")


def _migrate(con: sqlite3.Connection) -> None:
    if con.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
        return
    if not _pk_has_run_id(con, "classifications"):
        _rebuild_with_run_id_pk(con, "classifications", CLASSIFICATION_COLUMNS)
    if not _pk_has_run_id(con, "dedup_verdicts"):
        _rebuild_with_run_id_pk(con, "dedup_verdicts", DEDUP_COLUMNS)
    con.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    con.commit()
```

Note: `_rebuild_with_run_id_pk` renames the old table first, so the `IF NOT
EXISTS` CREATE inside `executescript(SCHEMA)` finds no `classifications` /
`dedup_verdicts` and creates the new-PK version. The other `CREATE … IF NOT
EXISTS` statements in `SCHEMA` are harmless no-ops.

- [ ] **Step 4: Call `_migrate` from `connect()`**

In `connect()`, insert the migration call between `executescript(SCHEMA)` and
`executescript(SCHEMA_VIEWS)`:

```python
    con.executescript(SCHEMA)
    _migrate(con)
    con.executescript(SCHEMA_VIEWS)
```

- [ ] **Step 5: Run the migration tests**

Run: `uv run pytest tests/triage_verse/test_db_migrate.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/db.py tests/triage_verse/test_db_migrate.py
git commit -m "feat(db): migrate existing decision tables to run_id-keyed history (#20)"
```

---

### Task 3: Read `tier1` "fixed" detection from the latest view

`tier1.py` decides whether an open issue looks fixed. Its subquery must consult
only the current classification, not stale history rows.

**Files:**
- Modify: `src/triage_verse/tier1.py` (the `EXISTS (… FROM classifications …)`
  subquery, around line 27)
- Test: `tests/triage_verse/test_tier1.py` (add a case; create the file if it
  does not yet exist — check first with `ls tests/triage_verse/`)

**Interfaces:**
- Consumes: `classifications_latest` view (Task 1).
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Inspect the `tier1` entry point first (`rg "^def " src/triage_verse/tier1.py`)
to get the exact function name and signature, then add a test that:

1. Inserts an OPEN issue `r/r#1` into `issues`.
2. Inserts an **older** classification (`run1`, earlier `at`) with a
   `close_candidate_json` whose `reason` is `"fixed"`.
3. Inserts a **newer** classification (`run2`, later `at`) with
   `close_candidate_json = NULL`.
4. Asserts the issue is **not** flagged as fixed — the latest verdict wins.

Example (adjust the call to match the real entry point):

```python
from triage_verse import db, tier1


def test_tier1_uses_latest_classification(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, {
        "repo": "r/r", "number": 1, "title": "t", "body": None,
        "state": "OPEN", "state_reason": None, "author": "a",
        "labels_json": "[]", "assignees_json": "[]", "milestone": None,
        "comment_count": 0, "reaction_count": 0, "is_pr": 0,
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z", "closed_at": None,
    })
    common = {
        "repo": "r/r", "number": 1, "clf_hash": "h", "type": "fix",
        "priority": "High", "assessment": "actionable", "labels_json": "[]",
        "confidence": 0.9, "model": "m",
    }
    db.upsert_classification(con, {
        **common, "close_candidate_json": '{"reason":"fixed"}',
        "run_id": "run1", "at": "2026-06-01T00:00:00Z"})
    db.upsert_classification(con, {
        **common, "close_candidate_json": None,
        "run_id": "run2", "at": "2026-06-02T00:00:00Z"})

    flagged = tier1.<entry_point>(con, ["r/r"])  # replace with real call
    assert ("r/r", 1) not in {(r["repo"], r["number"]) for r in flagged}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_tier1.py::test_tier1_uses_latest_classification -v`
Expected: FAIL — the base-table `EXISTS` still sees `run1`'s "fixed" candidate
and flags the issue.

- [ ] **Step 3: Repoint the subquery at the latest view**

In `src/triage_verse/tier1.py`, change the subquery's table:

```sql
            EXISTS (
              SELECT 1 FROM classifications_latest c
              WHERE c.repo = i.repo AND c.number = i.number
                AND c.close_candidate_json IS NOT NULL
                AND json_extract(c.close_candidate_json, '$.reason') = 'fixed'
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/triage_verse/test_tier1.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/tier1.py tests/triage_verse/test_tier1.py
git commit -m "fix(tier1): judge fixed-candidate from latest classification (#20)"
```

---

### Task 4: Full-suite gate and history-query smoke check

Confirm the whole pipeline still passes and prove the history query works
end-to-end.

**Files:**
- Test: `tests/triage_verse/test_db_analysis.py` (add one query test)

- [ ] **Step 1: Add a full-history query test**

```python
def test_full_history_query(tmp_path):
    con = _con(tmp_path)
    base = {
        "repo": "r/r", "number": 1, "clf_hash": "h", "type": "fix",
        "priority": "High", "assessment": "actionable", "labels_json": "[]",
        "close_candidate_json": None, "confidence": 0.9, "model": "m1",
        "run_id": "run1", "at": "2026-06-01T00:00:00Z",
    }
    db.upsert_classification(con, base)
    db.upsert_classification(
        con, {**base, "run_id": "run2", "model": "m2",
              "priority": "Low", "at": "2026-06-08T00:00:00Z"})
    history = con.execute(
        "SELECT run_id, model, priority, at FROM classifications "
        "WHERE repo='r/r' AND number=1 ORDER BY at"
    ).fetchall()
    assert [(r["run_id"], r["model"], r["priority"]) for r in history] == [
        ("run1", "m1", "High"), ("run2", "m2", "Low")]
```

- [ ] **Step 2: Run the new test**

Run: `uv run pytest tests/triage_verse/test_db_analysis.py::test_full_history_query -v`
Expected: PASS.

- [ ] **Step 3: Run the full Python gate**

Run: `make py-check`
Expected: ruff (format + lint), pyright, and pytest all pass. If any
`test_analyze.py` count assertion fails, inspect it: single-run analyze produces
one row per key, so counts should be unchanged. Only adjust an assertion if it
provably relied on overwrite semantics (document why in the commit). Do not
loosen an assertion to hide a real regression.

- [ ] **Step 4: Commit**

```bash
git add tests/triage_verse/test_db_analysis.py
git commit -m "test(db): assert full decision history is queryable (#20)"
```

---

## Self-Review

**Spec coverage:**
- Widen PK / append instead of upsert → Task 1 (steps 3, 6) + Global Constraint
  on recheck.
- `*_latest` views + read paths switch → Task 1 (steps 4–5, 7) and Task 3
  (`tier1`). `proposals.build` intentionally excluded (Global Constraints).
- Migration preserving rows → Task 2.
- History queryable per issue/pair → Task 4 (+ Task 1 append tests).
- Existing tests pass → Task 4 step 3 (`make py-check`).

**Placeholder scan:** The only intentional blank is the `tier1` entry-point name
in Task 3, which the step explicitly instructs the engineer to discover with
`rg` before writing the test — the real function name is not knowable without
reading the file and must not be guessed.

**Type consistency:** `_migrate`, `_pk_has_run_id`, `_rebuild_with_run_id_pk`,
`SCHEMA_VIEWS`, `_SCHEMA_VERSION` are defined in Task 2 and referenced only
there. `CLASSIFICATION_COLUMNS` / `DEDUP_COLUMNS` already exist in `db.py`. Key
tuples in `upsert_*` match the PK column order in `SCHEMA`.
