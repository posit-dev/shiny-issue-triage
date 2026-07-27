"""Record human review decisions (approve/reject/skip/edit) on proposals into a JSONL log."""

from __future__ import annotations

import functools
import os
import pathlib
import uuid
from datetime import datetime, timezone

from . import gh, jsonl_log


@functools.cache
def current_actor() -> str:
    """Resolve the local reviewer's identity for decision attribution.

    Tries the GitHub login (a REST read that passes the egress guard), then
    `$USER`, then "unknown". Cached: resolved once per process. Never raises.
    """
    try:
        login = gh.run_gh(["api", "user", "--jq", ".login"], retries=1).strip()
        if login:
            return login
    except Exception:
        pass
    return os.environ.get("USER") or "unknown"


def record(
    proposal: dict,
    verdict: str,
    *,
    params: dict | None = None,
    decided_by: str,
    reason: str | None = None,
) -> dict:
    rec = {
        "id": uuid.uuid4().hex,
        "proposal_id": proposal["id"],
        "repo": proposal["repo"],
        "issue": proposal["issue"],
        "action": proposal["action"],
        "params": proposal["params"] if params is None else params,
        "verdict": verdict,
        "confidence": proposal.get("confidence"),
        "decided_by": decided_by,
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if params is not None:
        rec["proposed_params"] = proposal["params"]
    if reason:
        rec["reason"] = reason
    return rec


def record_transferred(decision: dict, *, decided_by: str) -> dict:
    """Close out an approved suggest-transfer once a human has moved the issue.

    Takes the *approved decision* rather than a proposal, since that is what the
    Transfers worklist holds. `record` reads `proposal["id"]`, so the decision's
    `proposal_id` is mapped onto `id` to keep both records pointing at the same
    proposal.

    `decided_by` is whoever confirmed the move, which is not necessarily whoever
    approved the suggestion -- so it is supplied fresh rather than copied off
    `decision`.
    """
    from . import review_queue

    return record(
        {**decision, "id": decision["proposal_id"]},
        review_queue.TRANSFER_DONE_VERDICT,
        decided_by=decided_by,
    )


def write(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    return jsonl_log.append_weekly(records, base_dir, today=today)
