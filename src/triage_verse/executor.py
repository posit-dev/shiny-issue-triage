"""Apply approved review decisions to GitHub, with batch undo."""

from __future__ import annotations

from datetime import datetime, timezone

FINAL_STATUSES = frozenset({"applied", "stale-needs-rereview", "error"})
EXECUTABLE_VERDICTS = frozenset({"approved", "edited"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_executable(decisions: list[dict], results: list[dict]) -> list[dict]:
    """Latest approved/edited decision per proposal, minus already-finalized ones."""
    latest: dict[str, dict] = {}
    for d in decisions:
        pid = d.get("proposal_id")
        if pid is None:
            continue
        cur = latest.get(pid)
        if cur is None or d.get("decided_at", "") > cur.get("decided_at", ""):
            latest[pid] = d
    finalized = {
        r["decision_id"]
        for r in results
        if r.get("status") in FINAL_STATUSES and "decision_id" in r
    }
    picked = [
        d
        for d in latest.values()
        if d.get("verdict") in EXECUTABLE_VERDICTS and d["id"] not in finalized
    ]
    return sorted(picked, key=lambda d: (d.get("decided_at", ""), d["id"]))


def index_proposals(proposals: list[dict]) -> dict[str, dict]:
    return {p["id"]: p for p in proposals if "id" in p}
