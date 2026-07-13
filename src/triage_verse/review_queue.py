"""Load undecided review-queue proposals, sorted by confidence."""

from __future__ import annotations

import json
import logging
import pathlib
import re
import sqlite3

from . import db

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = frozenset({"add-label", "set-priority", "close", "close-duplicate"})
# Actions that must be judged from the full-evidence drawer, never a row snippet
# or bulk approve.
HIGH_STAKES_ACTIONS = frozenset({"close", "close-duplicate"})

# Browser KeyboardEvent.key -> review action. Mirrored into the app's JS
# keydown listener via json.dumps so the binding lives in exactly one place.
KEY_ACTIONS = {
    "j": "next",
    "k": "prev",
    "a": "approve",
    "r": "reject",
    "s": "skip",
    "e": "edit",
    "o": "open",
    "Enter": "open",
    "Escape": "close",
}


def clamp_index(index: int | None, length: int) -> int | None:
    """Clamp a selection index to a queue of `length`; None means no selection."""
    if length <= 0:
        return None
    if index is None:
        return 0
    return max(0, min(index, length - 1))


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
