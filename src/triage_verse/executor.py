"""Apply approved review decisions to GitHub, with batch undo."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import templates as templates_mod

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


PRIORITY_VALUES = ("Critical", "High", "Medium", "Low")
CLOSE_REASON_MAP = {
    "fixed": ("completed", "close-completed"),
    "answered": ("completed", "close-completed"),
    "stale": ("not planned", "close-not-planned"),
    "not-planned": ("not planned", "close-not-planned"),
}

_REF_FULL = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_REF_URL = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)$")
_REF_LOCAL = re.compile(r"^#?(\d+)$")


def parse_issue_ref(text: str, default_repo: str) -> tuple[str, int] | None:
    text = text.strip()
    for pattern in (_REF_FULL, _REF_URL):
        m = pattern.match(text)
        if m:
            return m.group(1), int(m.group(2))
    m = _REF_LOCAL.match(text)
    if m:
        return default_repo, int(m.group(1))
    return None


def _issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def plan_decision(
    decision: dict, issue: dict, *, allowed: set[str], tmpl: dict[str, str]
) -> tuple[list[dict], str | None]:
    """Turn one decision into allowlisted mutations, or an error message."""
    action = decision["action"]
    params = decision.get("params") or {}

    if action == "add-label":
        label = params.get("label")
        if label not in allowed:
            return [], f"label not in allowlist: {label!r}"
        return [{"kind": "add-label", "label": label}], None

    if action == "set-priority":
        priority = params.get("priority")
        if priority not in PRIORITY_VALUES:
            return [], f"unknown priority: {priority!r}"
        label = f"Priority: {priority}"
        if label not in allowed:
            return [], f"label not in allowlist: {label!r}"
        current = [entry["name"] for entry in issue.get("labels", [])]
        muts: list[dict] = [
            {"kind": "remove-label", "label": name}
            for name in current
            if name.startswith("Priority: ") and name != label
        ]
        muts.append({"kind": "add-label", "label": label})
        return muts, None

    if action == "close":
        reason = params.get("reason")
        if reason == "duplicate":
            return [], "close reason 'duplicate' must arrive as a close-duplicate proposal"
        if reason not in CLOSE_REASON_MAP:
            return [], f"unknown close reason: {reason!r}"
        gh_reason, template_name = CLOSE_REASON_MAP[reason]
        body = templates_mod.render(tmpl, template_name)
        return [
            {"kind": "comment", "body": body},
            {"kind": "close", "reason": gh_reason},
        ], None

    if action == "close-duplicate":
        canonical = params.get("canonical")
        if not canonical:
            return [], "close-duplicate requires a canonical target"
        ref = parse_issue_ref(str(canonical), decision["repo"])
        if ref is None:
            return [], f"cannot parse canonical issue ref: {canonical!r}"
        if ref == (decision["repo"], decision["issue"]):
            return [], "canonical target is the issue itself"
        url = _issue_url(*ref)
        if ref[0] == decision["repo"]:
            body = templates_mod.render(tmpl, "close-duplicate", canonical_url=url)
            return [
                {"kind": "comment", "body": body},
                {"kind": "close-duplicate", "canonical": [ref[0], ref[1]]},
            ], None
        body = templates_mod.render(
            tmpl, "close-duplicate-cross-repo", canonical_url=url
        )
        return [
            {"kind": "comment", "body": body},
            {"kind": "close", "reason": "not planned"},
        ], None

    return [], f"action not allowlisted: {action!r}"
