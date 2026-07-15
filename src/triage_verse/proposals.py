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


def prune_proposals(
    proposals_dir: str | pathlib.Path,
    target: str,
    *,
    apply: bool = False,
) -> list[dict]:
    """Remove proposal records with an invalid Shiny module id.

    `target` is either a proposal id or a path to a proposals ``.jsonl`` file:

    - if it names an existing file, remove every record *in that file* whose
      ``id`` is not a valid module id (this also catches missing/empty ids);
    - otherwise treat it as a proposal id and remove the record(s) with that
      exact id -- refusing (``ValueError``) if the id is itself a valid module
      id, so a well-formed proposal can never be deleted this way.

    Real ids are ``uuid4().hex`` and always valid; an invalid one only comes
    from a hand-edited record. Deleting it and re-running ``analyze`` mints a
    fresh valid id (GitHub is the source of truth).

    Returns one match dict ``{"file", "line", "record"}`` per removed record
    (or, when ``apply`` is False, per record that *would* be removed). Files are
    rewritten line-for-line, dropping only matched lines; every other line --
    including blank and malformed-JSON lines -- is preserved verbatim.
    """
    from . import review_queue

    target_path = pathlib.Path(target)
    file_mode = target_path.is_file()
    if not file_mode and review_queue.valid_module_id(target):
        raise ValueError(f"'{target}' is a valid module id; nothing to prune")

    if file_mode:
        files = [target_path]
    else:
        base = pathlib.Path(proposals_dir)
        files = sorted(base.glob("**/*.jsonl")) if base.exists() else []

    def _should_remove(rec: dict) -> bool:
        if file_mode:
            return not review_queue.valid_module_id(rec.get("id"))
        return rec.get("id") == target

    removed: list[dict] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept: list[str] = []
        changed = False
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            rec = None
            if stripped:
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    rec = None
            if isinstance(rec, dict) and _should_remove(rec):
                removed.append({"file": str(path), "line": lineno, "record": rec})
                changed = True
                continue
            kept.append(line)
        if apply and changed:
            path.write_text("".join(kept), encoding="utf-8")
    return removed
