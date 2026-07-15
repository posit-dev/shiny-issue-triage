"""Graduated autonomy: per-category precision, promotion, demotion."""

from __future__ import annotations

ELIGIBLE = ("add-label", "set-priority")
_SUCCESS = {"approved", "edited"}
_FAILURE = {"rejected"}


def category_precision(decisions: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for action in {d.get("action") for d in decisions}:
        if action is None:
            continue
        judged = [
            d
            for d in decisions
            if d.get("action") == action
            and d.get("verdict") in _SUCCESS | _FAILURE
            and d.get("decided_by") != "autonomy"
        ]
        if not judged:
            continue
        ok = sum(1 for d in judged if d["verdict"] in _SUCCESS)
        out[action] = {"reviewed": len(judged), "precision": ok / len(judged)}
    return out


def evaluate(decisions: list[dict], results: list[dict], cfg) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for action in ELIGIBLE:
        judged = [
            d
            for d in decisions
            if d.get("action") == action
            and d.get("verdict") in _SUCCESS | _FAILURE
            and d.get("decided_by") != "autonomy"
        ]
        audit_failures = sum(
            1
            for r in results
            if r.get("action") == action and r.get("audit_verdict") == "rejected"
        )
        total = len(judged) + audit_failures
        if total == 0:
            continue
        ok = sum(1 for d in judged if d["verdict"] in _SUCCESS)
        precision = ok / total
        out[action] = {
            "reviewed": len(judged),
            "precision": precision,
            "audit_failures": audit_failures,
            "promote": len(judged) >= cfg.min_decisions
            and precision >= cfg.min_precision,
        }
    return out


def render_config(evaluated: dict[str, dict], cfg, *, today: str) -> dict:
    promoted = {
        action: {"promoted_at": today, "confidence_floor": cfg.confidence_floor}
        for action, ev in evaluated.items()
        if ev.get("promote")
    }
    return {"promoted": promoted}
