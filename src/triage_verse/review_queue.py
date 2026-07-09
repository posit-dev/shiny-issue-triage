"""Load undecided review-queue proposals, sorted by confidence."""

from __future__ import annotations

import json
import logging
import pathlib
import re
import sqlite3

from . import db

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = frozenset(
    {"add-label", "set-priority", "close", "close-duplicate"}
)
# Actions that must be judged from the full-evidence drawer, never a row snippet
# or bulk approve.
HIGH_STAKES_ACTIONS = frozenset({"close", "close-duplicate"})


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


_EVIDENCE_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/issues/(\d+)$")


def duplicate_sibling(proposal: dict) -> tuple[str, int] | None:
    """The other issue of a close-duplicate pair, from the proposal's evidence URLs."""
    for url in proposal.get("evidence") or []:
        m = _EVIDENCE_URL.match(url)
        if m is None:
            continue
        repo, number = m.group(1), int(m.group(2))
        if (repo, number) != (proposal["repo"], proposal["issue"]):
            return repo, number
    return None


def issue_snippet(title: str, body: str | None, max_chars: int = 280) -> str:
    body = (body or "").strip()
    if not body:
        return title
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return f"{title}\n\n{body}"
