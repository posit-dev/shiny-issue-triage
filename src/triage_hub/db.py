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
