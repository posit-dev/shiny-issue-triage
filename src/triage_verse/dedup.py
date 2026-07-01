"""Duplicate-pair adjudication: schema, request building, parsing, storage."""

from __future__ import annotations

import json
import sqlite3

from . import config, db, llm, prompts

DEDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["duplicate", "related", "distinct"]},
        "canonical": {"type": ["string", "null"]},
        "cross_repo_option": {
            "type": ["string", "null"],
            "enum": ["close-and-link", "transfer", "keep-both-link", None],
        },
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": [
        "verdict",
        "canonical",
        "cross_repo_option",
        "confidence",
        "rationale",
    ],
    "additionalProperties": False,
}


def _issue_block(con: sqlite3.Connection, repo: str, number: int) -> str:
    row = con.execute(
        "SELECT title, body FROM issues WHERE repo=? AND number=?", (repo, number)
    ).fetchone()
    title = row["title"] if row else ""
    body = row["body"] if row else ""
    return (
        f"{repo}#{number}\n"
        + prompts.delimit("ISSUE_TITLE", title)
        + "\n"
        + prompts.delimit("ISSUE_BODY", body)
    )


def build_requests(
    con: sqlite3.Connection,
    stage: config.StageConfig,
    system: list[dict[str, object]],
    pairs: list,
    prefix: str = "d",
) -> list[llm.BatchRequest]:
    reqs = []
    for i, (a, b) in enumerate(pairs):
        content = "\n\n".join(
            [
                "Issue A:",
                _issue_block(con, a[0], a[1]),
                "Issue B:",
                _issue_block(con, b[0], b[1]),
                "Decide whether A and B are duplicate, related, or distinct. "
                "Respond with JSON matching the schema.",
            ]
        )
        reqs.append(
            llm.BatchRequest(
                custom_id=f"{prefix}{i}",
                params={
                    "model": stage.model,
                    "max_tokens": stage.max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": content}],
                    "output_config": llm.output_config_for(DEDUP_SCHEMA),
                },
            )
        )
    return reqs


def parse(result: llm.BatchResult) -> dict | None:
    if result.status != "succeeded":
        return None
    try:
        return llm.extract_json(result.message)
    except (StopIteration, ValueError):
        return None


def store(
    con: sqlite3.Connection, pair: tuple, data: dict, model: str, run_id: str
) -> None:
    a, b = pair
    db.upsert_dedup_verdict(
        con,
        {
            "repo_a": a[0],
            "number_a": a[1],
            "repo_b": b[0],
            "number_b": b[1],
            "hash_a": a[2],
            "hash_b": b[2],
            "verdict": data["verdict"],
            "canonical_json": json.dumps(data.get("canonical")),
            "cross_repo_option": data.get("cross_repo_option"),
            "confidence": data["confidence"],
            "rationale": data["rationale"],
            "model": model,
            "run_id": run_id,
            "at": db._now(),
        },
    )
    con.commit()
