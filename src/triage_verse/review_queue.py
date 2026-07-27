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
    {
        "add-label",
        "set-priority",
        "close",
        "close-duplicate",
        "link-duplicate",
        "suggest-transfer",
    }
)
# Actions that must be judged from the full-evidence drawer, never a row snippet
# or bulk approve.
HIGH_STAKES_ACTIONS = frozenset({"close", "close-duplicate"})

# Browser KeyboardEvent.key -> review action. Mirrored into the app's JS
# keydown listener via json.dumps so the binding lives in exactly one place.
KEY_ACTIONS = {
    "j": "next",
    "k": "prev",
    "ArrowDown": "next",
    "ArrowUp": "prev",
    "a": "approve",
    "r": "reject",
    "s": "skip",
    "e": "edit",
    "Enter": "toggle",
    "Escape": "close",
}


# A proposal's `id` is used verbatim as a Shiny dynamic-module namespace in the
# review app (`row_ui`/`row_server`). Shiny's `validate_id` only accepts ids
# matching `^\.?\w+$`, and raises `ValueError` at render time otherwise -- which,
# because the whole queue renders in one output, would blank every row, not just
# the offending one. Real ids are `uuid4().hex` (always safe), so we simply drop
# any id that isn't (with a warning) rather than let one bad row take down the
# queue. Kept in sync with shiny._namespaces.re_valid_id.
_VALID_MODULE_ID = re.compile(r"\.?\w+")


def valid_module_id(id: object) -> bool:
    """True if `id` is usable as a Shiny module namespace (see `_VALID_MODULE_ID`)."""
    return isinstance(id, str) and _VALID_MODULE_ID.fullmatch(id) is not None


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


def _not_reviewable(con: sqlite3.Connection, repo: str, number: int) -> bool:
    """True when the mirror says this issue cannot be reviewed.

    Either it is closed, or it is absent -- which means sync reconciliation
    retired it because GitHub no longer lists it in this repo (transferred away
    or deleted), so any proposal about it is moot.
    """
    issue = db.get_issue(con, repo, number)
    return issue is None or issue["state"] != "OPEN"


# Recorded when a human confirms they moved an issue a suggest-transfer proposal
# pointed at. Terminal so the proposal cannot bounce back into the main queue.
TRANSFER_DONE_VERDICT = "transferred"
# Verdicts that remove a proposal from the queue for good (subject to a stale
# bounce). "skipped" is deliberately NOT here: skip means "not now", so the
# proposal is kept and merely demoted (see below).
TERMINAL_VERDICTS = frozenset({"approved", "edited", "rejected", TRANSFER_DONE_VERDICT})


def _issue_updated_after(
    con: sqlite3.Connection, repo: str, number: int, when: str
) -> bool:
    """True if the mirrored issue's updated_at is newer than `when`."""
    issue = db.get_issue(con, repo, number)
    return issue is not None and (issue["updated_at"] or "") > when


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
    # Latest decision (time + verdict) per proposal.
    latest: dict[str, tuple[str, str | None]] = {}
    for r in iter_jsonl_records(decisions_dir):
        pid = r.get("proposal_id")
        if pid is None:
            continue
        t = r.get("decided_at", "")
        if pid not in latest or t >= latest[pid][0]:
            latest[pid] = (t, r.get("verdict"))

    terminal_ids: set[str] = set()
    skipped_at: dict[str, str] = {}
    for pid, (t, verdict) in latest.items():
        # A newer stale bounce voids any decision; the proposal resurfaces fresh.
        if stale_at.get(pid, "") > t:
            continue
        if verdict in TERMINAL_VERDICTS:
            terminal_ids.add(pid)
        elif verdict == "skipped":
            skipped_at[pid] = t

    proposals = []
    for r in iter_jsonl_records(proposals_dir):
        pid = r.get("id")
        if not valid_module_id(pid):
            logger.warning(
                "skipping proposal %r (%s#%s): invalid Shiny module id. "
                "Remove it with 'triage-verse proposals prune %s' (or pass its "
                ".jsonl file), then re-run 'triage-verse analyze'.",
                pid,
                r.get("repo"),
                r.get("issue"),
                pid,
            )
            continue
        if (
            pid in terminal_ids
            or r.get("action") not in SUPPORTED_ACTIONS
            or _not_reviewable(con, r["repo"], r["issue"])
        ):
            continue
        rec = {**r, "stale": True} if pid in stale_at else dict(r)
        skip_t = skipped_at.get(pid) if isinstance(pid, str) else None
        # A "not now" skip keeps the existing analysis and stays in the queue,
        # demoted to the bottom -- UNLESS the issue has been updated since the
        # skip, in which case it deserves a fresh look at full priority.
        if skip_t is not None and not _issue_updated_after(
            con, r["repo"], r["issue"], skip_t
        ):
            rec["deferred"] = True
        proposals.append(rec)
    return sorted(
        proposals,
        key=lambda r: (r.get("deferred", False), -(r.get("confidence") or 0.0)),
    )


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


_CANONICAL_REF = re.compile(
    r"^(?:([\w.-]+/[\w.-]+)#\d+|https://github\.com/([\w.-]+/[\w.-]+)/issues/\d+)$"
)


def transfer_destination(record: dict) -> str | None:
    """Repo a suggest-transfer points at, from `params.canonical`.

    Works for proposal and decision records alike: decisions carry `params` but
    not `evidence`, so the canonical ref is the only shared source.
    """
    canonical = (record.get("params") or {}).get("canonical")
    if not isinstance(canonical, str):
        return None
    m = _CANONICAL_REF.match(canonical.strip())
    if m is None:
        return None
    return m.group(1) or m.group(2)


def pending_transfers(decisions_dir: str | pathlib.Path) -> list[dict]:
    """Approved suggest-transfer decisions not yet marked transferred, newest first."""
    latest: dict[str, dict] = {}
    for r in iter_jsonl_records(decisions_dir):
        pid = r.get("proposal_id")
        if pid is None or r.get("action") != "suggest-transfer":
            continue
        cur = latest.get(pid)
        if cur is None or r.get("decided_at", "") >= cur.get("decided_at", ""):
            latest[pid] = r
    return sorted(
        (d for d in latest.values() if d.get("verdict") in ("approved", "edited")),
        key=lambda d: d.get("decided_at", ""),
        reverse=True,
    )


def issue_snippet(title: str, body: str | None, max_chars: int = 280) -> str:
    body = (body or "").strip()
    if not body:
        return title
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return f"{title}\n\n{body}"
