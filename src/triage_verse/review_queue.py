"""Load undecided review-queue proposals, sorted by confidence."""

from __future__ import annotations

import json
import logging
import pathlib
import sqlite3

from . import db

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = frozenset({"add-label", "set-priority"})


def iter_jsonl_records(base_dir: str | pathlib.Path) -> list[dict]:
    base = pathlib.Path(base_dir)
    if not base.exists():
        return []
    records: list[dict] = []
    for path in sorted(base.glob("**/*.jsonl")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping malformed JSON line %s:%d", path, lineno)
    return records


def _is_closed(con: sqlite3.Connection, repo: str, number: int) -> bool:
    issue = db.get_issue(con, repo, number)
    return issue is not None and issue["state"] != "OPEN"


def load_undecided(
    proposals_dir: str | pathlib.Path,
    decisions_dir: str | pathlib.Path,
    con: sqlite3.Connection,
) -> list[dict]:
    decided_ids = {
        r["proposal_id"]
        for r in iter_jsonl_records(decisions_dir)
        if "proposal_id" in r
    }
    proposals = [
        r
        for r in iter_jsonl_records(proposals_dir)
        if r.get("id") not in decided_ids
        and r.get("action") in SUPPORTED_ACTIONS
        and not _is_closed(con, r["repo"], r["issue"])
    ]
    return sorted(proposals, key=lambda r: r.get("confidence", 0.0), reverse=True)


def issue_snippet(title: str, body: str | None, max_chars: int = 280) -> str:
    body = (body or "").strip()
    if not body:
        return title
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return f"{title}\n\n{body}"
