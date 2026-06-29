"""Issue classification: schema, request building, parsing, recheck, storage."""

from __future__ import annotations

import hashlib
import json

from . import db, llm, prompts

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [
                "build",
                "chore",
                "ci",
                "docs",
                "feat",
                "fix",
                "perf",
                "refactor",
                "release",
                "style",
                "test",
                "question",
            ],
        },
        "priority": {"type": "string", "enum": ["Critical", "High", "Medium", "Low"]},
        "assessment": {
            "type": "string",
            "enum": [
                "actionable",
                "needs-info",
                "stale",
                "likely-fixed",
                "out-of-scope",
            ],
        },
        "labels": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "regression",
                    "duplicate",
                    "wrong location",
                    "needs reprex",
                    "needs clarification",
                ],
            },
        },
        "close_candidate": {
            "type": ["object", "null"],
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["duplicate", "stale", "not-planned", "fixed", "answered"],
                },
                "rationale": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["reason", "rationale", "confidence"],
            "additionalProperties": False,
        },
        "confidence": {"type": "number"},
    },
    "required": [
        "type",
        "priority",
        "assessment",
        "labels",
        "close_candidate",
        "confidence",
    ],
    "additionalProperties": False,
}


def clf_hash(title: str, body: str, comments: list[str]) -> str:
    payload = (title or "") + (body or "") + "".join(comments)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _user_content(
    title: str, body: str | None, comments: list[str] | None = None
) -> str:
    parts = [prompts.delimit("ISSUE_TITLE", title), prompts.delimit("ISSUE_BODY", body)]
    if comments:
        parts.append(prompts.delimit("COMMENTS", "\n---\n".join(comments)))
    parts.append("Classify this issue. Respond with JSON matching the schema.")
    return "\n\n".join(parts)


def build_requests(
    con: object,
    cfg: object,
    stage: object,
    system: list[dict],
    issues: list[dict],
    prefix: str,
    *,
    with_comments: bool = False,
) -> list[llm.BatchRequest]:
    reqs = []
    for i, row in enumerate(issues):
        comments = (
            _recent_comments(con, row["repo"], row["number"]) if with_comments else None
        )  # type: ignore[arg-type]
        reqs.append(
            llm.BatchRequest(
                custom_id=f"{prefix}{i}",
                params={
                    "model": stage.model,  # type: ignore[attr-defined]
                    "max_tokens": stage.max_tokens,  # type: ignore[attr-defined]
                    "system": system,
                    "messages": [
                        {
                            "role": "user",
                            "content": _user_content(
                                row["title"], row.get("body"), comments
                            ),
                        }
                    ],
                    "output_config": llm.output_config_for(CLASSIFY_SCHEMA),
                },
            )
        )
    return reqs


def _recent_comments(con, repo: str, number: int, limit: int = 20) -> list[str]:
    rows = con.execute(
        "SELECT body FROM comments WHERE repo=? AND issue_number=? "
        "ORDER BY created_at DESC LIMIT ?",
        (repo, number, limit),
    ).fetchall()
    return [r["body"] or "" for r in reversed(rows)]


def parse(result: llm.BatchResult) -> dict | None:
    if result.status != "succeeded":
        return None
    try:
        return llm.extract_json(result.message)
    except (StopIteration, ValueError):
        return None


def needs_recheck(data: dict, floor: float) -> bool:
    return (
        data.get("confidence", 1.0) < floor or data.get("close_candidate") is not None
    )


def store(
    con,
    repo: str,
    number: int,
    hash_: str,
    data: dict,
    model: str,
    run_id: str,
    allowed: set[str],
) -> None:
    kept, _ = prompts.validate_labels(data.get("labels", []), allowed)
    cc = data.get("close_candidate")
    db.upsert_classification(
        con,
        {
            "repo": repo,
            "number": number,
            "clf_hash": hash_,
            "type": data["type"],
            "priority": data["priority"],
            "assessment": data["assessment"],
            "labels_json": json.dumps(kept),
            "close_candidate_json": json.dumps(cc) if cc else None,
            "confidence": data["confidence"],
            "model": model,
            "run_id": run_id,
            "at": db._now(),
        },
    )
    con.commit()
