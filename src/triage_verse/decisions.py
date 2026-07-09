"""Record human review decisions on proposals into a JSONL log."""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timezone

from . import jsonl_log


def record(proposal: dict, verdict: str) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "proposal_id": proposal["id"],
        "repo": proposal["repo"],
        "issue": proposal["issue"],
        "action": proposal["action"],
        "params": proposal["params"],
        "verdict": verdict,
        "confidence": proposal.get("confidence"),
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    return jsonl_log.append_weekly(records, base_dir, today=today)
