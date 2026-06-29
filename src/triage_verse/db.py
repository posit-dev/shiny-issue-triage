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
  PRIMARY KEY (repo, number)
);
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
  PRIMARY KEY (repo_a, number_a, repo_b, number_b)
);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  provider_batch_id TEXT,
  status TEXT NOT NULL,
  request_count INTEGER NOT NULL DEFAULT 0,
  submitted_at TEXT,
  ended_at TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS batch_items (
  batch_id TEXT NOT NULL,
  custom_id TEXT NOT NULL,
  target_json TEXT NOT NULL,
  PRIMARY KEY (batch_id, custom_id)
);
CREATE INDEX IF NOT EXISTS idx_issues_updated ON issues(repo, updated_at);
CREATE INDEX IF NOT EXISTS idx_comments_issue ON comments(repo, issue_number);
CREATE INDEX IF NOT EXISTS idx_spend_run ON spend(run_id);
CREATE INDEX IF NOT EXISTS idx_batches_open ON batches(status);
"""

ISSUE_COLUMNS = (
    "repo",
    "number",
    "title",
    "body",
    "state",
    "state_reason",
    "author",
    "labels_json",
    "assignees_json",
    "milestone",
    "comment_count",
    "reaction_count",
    "is_pr",
    "created_at",
    "updated_at",
    "closed_at",
)
PR_COLUMNS = (
    "repo",
    "number",
    "merged",
    "merged_at",
    "closing_issue_refs_json",
    "head_ref",
    "base_ref",
)
COMMENT_COLUMNS = (
    "repo",
    "issue_number",
    "comment_id",
    "author",
    "body",
    "created_at",
    "updated_at",
)

_CURSOR_KINDS = {
    "issues": "issues_cursor",
    "prs": "prs_cursor",
    "comments": "comments_cursor",
}

_BATCH_MUTABLE = frozenset({"status", "ended_at", "error", "provider_batch_id"})


def connect(path: str | pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def _upsert(
    con: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    key: tuple[str, ...],
    row: dict,
) -> None:
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
    con.execute(
        f"INSERT INTO repos (repo, {column}) VALUES (?, ?) "
        f"ON CONFLICT (repo) DO UPDATE SET {column}=excluded.{column}",
        (repo, value),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_run(con: sqlite3.Connection, kind: str) -> str:
    """Insert a run record and commit.

    Call BEFORE any upserts on this connection: this commit flushes any
    open transaction.
    """
    run_id = uuid.uuid4().hex
    con.execute(
        "INSERT INTO runs (run_id, kind, started_at) VALUES (?, ?, ?)",
        (run_id, kind, _now()),
    )
    con.commit()
    return run_id


def finish_run(con: sqlite3.Connection, run_id: str, summary: dict) -> None:
    """Update the run record and commit.

    This commit also flushes any uncommitted upserts on this connection.
    """
    con.execute(
        "UPDATE runs SET finished_at=?, summary_json=? WHERE run_id=?",
        (_now(), json.dumps(summary), run_id),
    )
    con.commit()


CLASSIFICATION_COLUMNS = (
    "repo",
    "number",
    "clf_hash",
    "type",
    "priority",
    "assessment",
    "labels_json",
    "close_candidate_json",
    "confidence",
    "model",
    "run_id",
    "at",
)
DEDUP_COLUMNS = (
    "repo_a",
    "number_a",
    "repo_b",
    "number_b",
    "hash_a",
    "hash_b",
    "verdict",
    "canonical_json",
    "cross_repo_option",
    "confidence",
    "rationale",
    "model",
    "run_id",
    "at",
)


def upsert_classification(con: sqlite3.Connection, row: dict) -> None:
    _upsert(con, "classifications", CLASSIFICATION_COLUMNS, ("repo", "number"), row)


def get_classification(
    con: sqlite3.Connection, repo: str, number: int
) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM classifications WHERE repo=? AND number=?", (repo, number)
    ).fetchone()


def upsert_dedup_verdict(con: sqlite3.Connection, row: dict) -> None:
    _upsert(
        con,
        "dedup_verdicts",
        DEDUP_COLUMNS,
        ("repo_a", "number_a", "repo_b", "number_b"),
        row,
    )


def get_dedup_verdict(
    con: sqlite3.Connection, repo_a: str, number_a: int, repo_b: str, number_b: int
) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM dedup_verdicts WHERE repo_a=? AND number_a=? AND repo_b=? AND number_b=?",
        (repo_a, number_a, repo_b, number_b),
    ).fetchone()


def insert_batch(
    con: sqlite3.Connection,
    batch_id: str,
    run_id: str,
    stage: str,
    provider_batch_id: str,
    request_count: int,
) -> None:
    con.execute(
        "INSERT INTO batches (batch_id, run_id, stage, provider_batch_id, status,"
        " request_count, submitted_at) VALUES (?, ?, ?, ?, 'submitted', ?, ?)",
        (batch_id, run_id, stage, provider_batch_id, request_count, _now()),
    )


def set_batch(con: sqlite3.Connection, batch_id: str, **fields: object) -> None:
    if not fields:
        raise ValueError("set_batch requires at least one field to update")
    unknown = set(fields) - _BATCH_MUTABLE
    if unknown:
        raise ValueError(f"set_batch got unknown field(s): {sorted(unknown)}")
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(
        f"UPDATE batches SET {cols} WHERE batch_id=?",
        (*fields.values(), batch_id),
    )


def open_batches(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM batches WHERE status='submitted' ORDER BY submitted_at"
    ).fetchall()


def run_batches(con: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM batches WHERE run_id=? ORDER BY submitted_at", (run_id,)
    ).fetchall()


def insert_batch_items(
    con: sqlite3.Connection, batch_id: str, items: dict[str, str]
) -> None:
    con.executemany(
        "INSERT INTO batch_items (batch_id, custom_id, target_json) VALUES (?, ?, ?)",
        [(batch_id, cid, tgt) for cid, tgt in items.items()],
    )


def get_batch_items(con: sqlite3.Connection, batch_id: str) -> dict[str, str]:
    rows = con.execute(
        "SELECT custom_id, target_json FROM batch_items WHERE batch_id=?", (batch_id,)
    ).fetchall()
    return {r["custom_id"]: r["target_json"] for r in rows}


def insert_spend(
    con: sqlite3.Connection,
    run_id: str,
    stage: str,
    model: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    usd: float,
) -> None:
    con.execute(
        "INSERT INTO spend (run_id, stage, model, input_tokens, cached_tokens,"
        " output_tokens, usd, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, stage, model, input_tokens, cached_tokens, output_tokens, usd, _now()),
    )


def today_spend_usd(con: sqlite3.Connection) -> float:
    day = _now()[:10]
    row = con.execute(
        "SELECT COALESCE(SUM(usd), 0.0) AS total FROM spend WHERE at >= ?",
        (day + "T00:00:00Z",),
    ).fetchone()
    return float(row["total"])
