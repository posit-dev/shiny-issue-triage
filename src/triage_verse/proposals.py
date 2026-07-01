"""Project cached classifications and dedup verdicts into a JSONL proposal log."""

from __future__ import annotations

import json
import pathlib
import uuid

from . import jsonl_log


def build(con, run_id: str) -> list[dict]:
    records: list[dict] = []
    clf_rows = con.execute(
        "SELECT c.*, i.updated_at AS issue_updated_at FROM classifications c "
        "JOIN issues i ON i.repo=c.repo AND i.number=c.number WHERE c.run_id=?",
        (run_id,),
    ).fetchall()
    for c in clf_rows:
        base = {
            "repo": c["repo"],
            "issue": c["number"],
            "issue_updated_at": c["issue_updated_at"],
            "run_id": run_id,
            "model": c["model"],
            "confidence": c["confidence"],
            "evidence": [f"https://github.com/{c['repo']}/issues/{c['number']}"],
        }
        for label in json.loads(c["labels_json"]):
            records.append(_rec(base, "add-label", {"label": label}, ""))
        records.append(_rec(base, "set-priority", {"priority": c["priority"]}, ""))
        if c["close_candidate_json"]:
            cc = json.loads(c["close_candidate_json"])
            records.append(
                _rec(
                    base,
                    "close",
                    {"reason": cc["reason"]},
                    cc.get("rationale", ""),
                    confidence=cc.get("confidence", c["confidence"]),
                )
            )

    dup_rows = con.execute(
        "SELECT d.*, i.updated_at AS issue_updated_at FROM dedup_verdicts d "
        "JOIN issues i ON i.repo=d.repo_a AND i.number=d.number_a "
        "WHERE d.run_id=? AND d.verdict='duplicate'",
        (run_id,),
    ).fetchall()
    for d in dup_rows:
        repo, num = d["repo_a"], d["number_a"]
        base = {
            "repo": repo,
            "issue": num,
            "issue_updated_at": d["issue_updated_at"],
            "run_id": run_id,
            "model": d["model"],
            "confidence": d["confidence"],
            "evidence": [
                f"https://github.com/{d['repo_a']}/issues/{d['number_a']}",
                f"https://github.com/{d['repo_b']}/issues/{d['number_b']}",
            ],
        }
        records.append(
            _rec(
                base,
                "close-duplicate",
                {
                    "canonical": json.loads(d["canonical_json"])
                    if d["canonical_json"]
                    else None,
                    "cross_repo_option": d["cross_repo_option"],
                },
                d["rationale"],
            )
        )
    return records


def _rec(
    base: dict,
    action: str,
    params: dict,
    rationale: str,
    confidence: float | None = None,
) -> dict:
    rec = dict(base)
    rec.update(
        {
            "id": uuid.uuid4().hex,
            "action": action,
            "params": params,
            "rationale": rationale,
        }
    )
    if confidence is not None:
        rec["confidence"] = confidence
    return rec


def write(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    return jsonl_log.append_weekly(records, base_dir, today=today)
