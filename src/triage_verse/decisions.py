"""Record human review decisions (approve/reject/skip/edit) on proposals into a JSONL log."""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timezone

from . import jsonl_log


def record(proposal: dict, verdict: str, *, params: dict | None = None) -> dict:
    rec = {
        "id": uuid.uuid4().hex,
        "proposal_id": proposal["id"],
        "repo": proposal["repo"],
        "issue": proposal["issue"],
        "action": proposal["action"],
        "params": proposal["params"] if params is None else params,
        "verdict": verdict,
        "confidence": proposal.get("confidence"),
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if params is not None:
        rec["proposed_params"] = proposal["params"]
    return rec


def write(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    return jsonl_log.append_weekly(records, base_dir, today=today)
