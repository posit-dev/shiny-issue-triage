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


def write(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    return jsonl_log.append_weekly(records, base_dir, today=today)
