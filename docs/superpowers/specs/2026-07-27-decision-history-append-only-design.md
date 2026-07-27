# Design: Append-only decision history for classifications and dedup verdicts

- **Date:** 2026-07-27
- **Issue:** [#20](https://github.com/posit-dev/shiny-issue-triage/issues/20) —
  `feat(db): retain decision history via append-only classifications and dedup verdicts`
- **Status:** Accepted

## Problem

The live SQLite mirror (`.data/mirror.sqlite`) should be the *complete* record
for analytics, so we never have to archive or post historical DB snapshots to
GitHub to answer "what did triage decide last week / in the previous run?"

Today the mirror is a *current-state* store for its two decision tables:

- `classifications` — primary key `(repo, number)`; `store()` upserts
  (`src/triage_verse/classify.py:157-186` via `db.upsert_classification`).
- `dedup_verdicts` — primary key `(repo_a, number_a, repo_b, number_b)`;
  `store()` upserts (`src/triage_verse/dedup.py:90-113` via
  `db.upsert_dedup_verdict`).

Each analyze run overwrites the prior verdict for a key. Both tables already
carry `run_id`, `at`, and `model` (plus `clf_hash` / `hash_a` / `hash_b`) —
everything needed to attribute a change to a run and a moment in time. The only
thing forcing the overwrite is the primary key.

This design converts the two decision tables to append-only history so prior
state — and *when/why it changed* — stays queryable in one DB. Scope is
intentionally limited to those two tables. Operational/batch state
(`batches.status`) is **not** in scope: `runs`, `spend`, and existing batch
timestamps already cover operational questions.

## Goals

- Re-running analysis on an issue whose content changed produces a **new** row
  rather than overwriting; the prior row remains.
- A single query retrieves the full verdict history for an issue or a dedup pair
  (prior states + `at` + `run_id` + `model`).
- Existing read behavior is unchanged: callers that want "the current verdict"
  keep getting exactly one row per key.
- Existing verdicts survive the schema change (migration preserves them as the
  first history entry per key).

## Non-goals

- Batch/run operational transition log (a separate issue only if a report needs
  batch turnaround / retry metrics).
- Full event-sourcing of all tables. Only the two decision tables change.
- Recording the intra-run classify→recheck transition as separate rows (see
  "History granularity" below — we keep one row per run).

## Design

### 1. Schema: widen the primary key with `run_id`

Both decision tables move from current-state to append-only history by adding
`run_id` to the primary key. No columns are added or removed; only the PK
changes.

- `classifications`: PK `(repo, number)` → **`(repo, number, run_id)`**
- `dedup_verdicts`: PK `(repo_a, number_a, repo_b, number_b)` →
  **`(repo_a, number_a, repo_b, number_b, run_id)`**

`db.SCHEMA` is updated so fresh databases are created with the new PKs directly.

### History granularity: one row per run

Within a *single* analyze run an issue can be stored twice — once by the
`classify` stage and again, refined, by the `recheck` stage. Both stores use the
**same** `run_id` (`analyze.py:507` calls `classify.store(... run_id ...)` for
both stages). We keep **one row per run**: the recheck verdict refines that
run's row rather than adding a second row.

Concretely, `store()` in both `classify.py` and `dedup.py` keeps its
`ON CONFLICT … DO UPDATE` behavior, but on the **widened key**. So:

- A store from a *new* run inserts a new history row (the key's `run_id`
  component differs).
- A second store within the *same* run (recheck) updates that run's row in
  place.

This matches the issue's framing of history as per-run ("what did it decide in
the previous run"). The pre-recheck value within a run is transient scratch
state, not history worth keeping; `runs` and `spend` already capture intra-run
mechanics. `db.upsert_classification` / `db.upsert_dedup_verdict` and the
generic `db._upsert` helper are unchanged in shape — they receive the widened
key tuple.

Note: the `clf_hash` cache still short-circuits unchanged issues
(`analyze.py:238` skips an issue whose latest classification has the same
`clf_hash`), so history grows only when an issue is actually (re)analyzed, not on
every run over an unchanged issue.

### 2. "Latest" views for current-verdict reads

Add two read-only views that return the most-recent row per key, tie-broken
deterministically by `at` then `rowid` (so two rows with the same second-
resolution `at` still resolve to the last-inserted one):

```sql
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
```

`SELECT c.*` from the base table alias exposes only the base-table columns — no
window-function bookkeeping column leaks into consumers.

### 3. Read paths switch to the latest views

Read paths that mean "the current verdict" switch to the views so behavior is
unchanged:

- `db.get_classification` → `SELECT * FROM classifications_latest WHERE repo=? AND number=?`.
  This covers the recheck-skip / `clf_hash` cache in `analyze.py:238` and
  `analyze.py:247`.
- `db.get_dedup_verdict` → `SELECT * FROM dedup_verdicts_latest WHERE …`.
  This covers the dedup cache in `candidates.py:70`.
- `tier1.py:27` — the `EXISTS (SELECT 1 FROM classifications c …)` subquery reads
  `classifications_latest` instead of the base table, so a "fixed" close
  candidate is judged from the current verdict only.

`proposals.build()` (`proposals.py:15`, `proposals.py:45`) **stays on the base
tables**, filtered by `WHERE run_id = ?`. It intentionally builds proposals from
*the run just completed*, not "latest overall." With one row per run (and
recheck refining that row), the `run_id` filter already yields exactly one row
per key, so behavior is unchanged. Filtering by `run_id` is the honest
expression of intent; a `_latest` view would be equivalent only because the
current run is always the newest.

### 4. Migration

There is no migration framework today: `db.connect()` runs
`executescript(SCHEMA)` with `CREATE TABLE IF NOT EXISTS`, so an existing table
is never altered. Changing a PK in SQLite requires a table rebuild. We add a
minimal versioned migration keyed on `PRAGMA user_version`.

`db.connect()` sequence becomes:

1. Load the sqlite-vec extension (unchanged).
2. `executescript(SCHEMA)` — creates the tables with the **new** PKs on a fresh
   DB; a no-op on an existing DB (tables already exist, possibly with old PKs).
3. `_migrate(con)` — if `PRAGMA user_version < 1`, rebuild each decision table
   whose PK is still the old shape:
   - `ALTER TABLE <t> RENAME TO <t>_old;`
   - `CREATE TABLE <t> ( … new PK … );`
   - `INSERT INTO <t> SELECT <columns> FROM <t>_old;`
   - `DROP TABLE <t>_old;`

   Existing rows become the first history entry per key — their `run_id` and
   `at` are already populated. After both tables are handled, set
   `PRAGMA user_version = 1`.
4. Create the `*_latest` views (`CREATE VIEW IF NOT EXISTS …`) and the
   `vec_issues` virtual table (unchanged).

Detection of the old PK shape uses `PRAGMA table_info(<t>)`: the `pk` column
gives each column's ordinal position within the primary key (0 = not part of the
PK). On the old schema `run_id` has `pk = 0`; on the new schema it is non-zero.
Gating on `user_version` makes the whole step idempotent and cheap on every
connect. Migration runs inside SQLite's WAL mode without special handling.

The mirror under `.data/` is derived data that can be rebuilt from GitHub if
migration ever misbehaves, but the migration preserves verdicts so no rebuild is
required.

## Testing

- **Append across runs.** `store()` the same key under two different `run_id`s →
  two rows in the base table; `*_latest` returns the row from the newer run.
- **Recheck within a run (one row per run).** `store()` classify then recheck
  with the same `run_id` → one row; the recheck values win.
- **Latest views.** With multiple runs present, each `*_latest` view returns
  exactly one row per key, matching pre-change reads.
- **History query.** A query over the base table returns the full ordered
  history (prior states + `at` + `run_id` + `model`) for an issue and for a pair.
- **Read paths.** `db.get_classification` / `db.get_dedup_verdict` return the
  latest verdict; `tier1` "fixed" detection uses the latest classification.
- **Migration.** Seed a DB whose decision tables use the old PK and hold rows,
  run `db.connect()`, and assert the rows are preserved as first history entries,
  the new PK is in place, and `user_version` is bumped to 1. Re-connecting is a
  no-op.
- **Existing suite.** `test_analyze.py` (count assertions), `test_classify.py`,
  `test_dedup.py`, and `test_db_analysis.py` still pass. Single-run counts are
  unaffected (one row per key per run); adjust any assertion that implicitly
  relied on overwrite semantics.

## Acceptance criteria

- Re-running analysis on an already-(re)classified issue produces a **new** row
  rather than overwriting; the prior row remains.
- `*_latest` views return exactly one row per key, matching pre-change behavior.
- All existing current-verdict read paths use the latest views; existing tests
  pass.
- A query can retrieve the full verdict history for a given issue / pair (prior
  states + `at` + `run_id` + `model`).
- Migration preserves existing verdicts.
