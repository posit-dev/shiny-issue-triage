"""Tier 1 "already fixed?" checks: candidate selection + read-only session."""

from __future__ import annotations

import json

from . import review_queue


def select_candidates(con, repos, *, proposals_dir, limit: int) -> list[dict]:
    seen_tier1 = {
        (r.get("repo"), r.get("issue"))
        for r in review_queue.iter_jsonl_records(proposals_dir)
        if r.get("origin") == "tier1"
    }
    placeholders = ",".join("?" for _ in repos)
    rows = con.execute(
        f"""
        SELECT i.repo AS repo, i.number AS number, i.updated_at AS updated_at
        FROM issues i
        WHERE i.state = 'OPEN' AND i.repo IN ({placeholders})
          AND (
            EXISTS (
              SELECT 1 FROM classifications c
              WHERE c.repo = i.repo AND c.number = i.number
                AND c.close_candidate_json IS NOT NULL
                AND json_extract(c.close_candidate_json, '$.reason') = 'fixed'
            )
            OR EXISTS (
              SELECT 1 FROM prs p
              WHERE p.repo = i.repo AND p.merged = 1
                AND EXISTS (
                  SELECT 1 FROM json_each(p.closing_issue_refs_json) je
                  WHERE je.value = i.number
                )
            )
          )
        ORDER BY i.updated_at ASC, i.number ASC
        """,
        list(repos),
    ).fetchall()
    out = []
    for row in rows:
        key = (row["repo"], row["number"])
        if key in seen_tier1:
            continue
        out.append({"repo": row["repo"], "issue": row["number"]})
        if len(out) >= limit:
            break
    return out
