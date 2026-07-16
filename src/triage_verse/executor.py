"""Apply approved review decisions to GitHub, with batch undo."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import gh as gh_mod
from . import jsonl_log, prompts, review_queue
from . import templates as templates_mod

RunGh = Callable[..., str]

FINAL_STATUSES = frozenset({"applied", "stale-needs-rereview", "error"})
EXECUTABLE_VERDICTS = frozenset({"approved", "edited", "auto-approved"})


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


AUTO_ELIGIBLE = ("add-label", "set-priority")


def load_autonomy(path) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    import yaml

    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc.get("promoted") or {}


def _audit_flag(proposal_id: str, audit_rate: float) -> bool:
    if audit_rate <= 0:
        return False
    h = int(hashlib.sha256(proposal_id.encode()).hexdigest()[:8], 16)
    return (h % 100) < round(audit_rate * 100)


def select_auto(proposals, decided_ids, promoted, *, audit_rate: float) -> list[dict]:
    out = []
    for p in proposals:
        if p.get("id") in decided_ids:
            continue
        action = p.get("action")
        if action not in AUTO_ELIGIBLE or action not in promoted:
            continue
        floor = promoted[action].get("confidence_floor", 1.0)
        if (p.get("confidence") or 0.0) < floor:
            continue
        out.append({**p, "audit": _audit_flag(p["id"], audit_rate)})
    return out


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
            return (
                [],
                "close reason 'duplicate' must arrive as a close-duplicate proposal",
            )
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


def _fetch_issue(run_gh: RunGh, repo: str, number: int) -> dict:
    return json.loads(run_gh(["api", f"repos/{repo}/issues/{number}"]))


def _prior(issue: dict) -> dict:
    return {
        "labels": [entry["name"] for entry in issue.get("labels", [])],
        "state": issue["state"],
        "state_reason": issue.get("state_reason"),
    }


def _describe(mutation: dict) -> str:
    kind = mutation["kind"]
    if kind in ("add-label", "remove-label"):
        return f"{kind} {mutation['label']!r}"
    if kind == "comment":
        first_line = mutation["body"].strip().splitlines()[0]
        return f"comment: {first_line[:60]}…"
    if kind == "close":
        return f"close --reason {mutation['reason']!r}"
    return (
        f"close as duplicate of {mutation['canonical'][0]}#{mutation['canonical'][1]}"
    )


_CLOSE_STATE_REASON = {"completed": "COMPLETED", "not planned": "NOT_PLANNED"}

_ADD_LABELS = gh_mod.ADD_LABELS_MUTATION
_REMOVE_LABELS = gh_mod.REMOVE_LABELS_MUTATION
_ADD_COMMENT = (
    "mutation($id: ID!, $body: String!) { addComment(input: {subjectId: $id,"
    " body: $body}) { commentEdge { node { databaseId } } } }"
)
_CLOSE_ISSUE = (
    "mutation($id: ID!, $reason: IssueClosedStateReason) { closeIssue("
    "input: {issueId: $id, stateReason: $reason}) { issue { id } } }"
)
_CLOSE_DUPLICATE = (
    "mutation($id: ID!, $dup: ID!) { closeIssue(input: {issueId: $id,"
    " stateReason: DUPLICATE, duplicateIssueId: $dup}) { issue { id } } }"
)
_REOPEN_ISSUE = (
    "mutation($id: ID!) { reopenIssue(input: {issueId: $id}) { issue { id } } }"
)
_DELETE_COMMENT = (
    "mutation($id: ID!) { deleteIssueComment(input: {id: $id}) { clientMutationId } }"
)


def _comment_node_id(run_gh: RunGh, repo: str, comment_id: int) -> str:
    out = run_gh(["api", f"repos/{repo}/issues/comments/{comment_id}"])
    return json.loads(out)["node_id"]


def _apply_mutation(
    run_gh: RunGh, repo: str, number: int, node_id: str, mutation: dict
) -> int | None:
    """Perform one mutation via guarded GraphQL; returns comment databaseId if any."""
    kind = mutation["kind"]
    if kind == "add-label":
        label_id = gh_mod.label_node_id(repo, mutation["label"], run_gh=run_gh)
        gh_mod.gh_mutation(
            "addLabelsToIssue",
            _ADD_LABELS,
            {"id": node_id, "labels": [label_id]},
            repos=[repo],
        )
    elif kind == "remove-label":
        label_id = gh_mod.label_node_id(repo, mutation["label"], run_gh=run_gh)
        gh_mod.gh_mutation(
            "removeLabelsFromIssue",
            _REMOVE_LABELS,
            {"id": node_id, "labels": [label_id]},
            repos=[repo],
        )
    elif kind == "comment":
        data = gh_mod.gh_mutation(
            "addComment",
            _ADD_COMMENT,
            {"id": node_id, "body": mutation["body"]},
            repos=[repo],
        )
        return data["addComment"]["commentEdge"]["node"]["databaseId"]
    elif kind == "close":
        gh_mod.gh_mutation(
            "closeIssue",
            _CLOSE_ISSUE,
            {"id": node_id, "reason": _CLOSE_STATE_REASON[mutation["reason"]]},
            repos=[repo],
        )
    elif kind == "close-duplicate":
        dup_repo, dup_number = mutation["canonical"]
        dup = _fetch_issue(run_gh, dup_repo, dup_number)
        gh_mod.gh_mutation(
            "closeIssue",
            _CLOSE_DUPLICATE,
            {"id": node_id, "dup": dup["node_id"]},
            repos=[repo],
        )
    return None


# Same GitHub → mirror mapping used when closing an issue (see _CLOSE_STATE_REASON).
_MIRROR_STATE_REASON = _CLOSE_STATE_REASON


def _update_mirror(
    con: sqlite3.Connection,
    repo: str,
    number: int,
    prior_labels: list[str],
    mutations: list[dict],
) -> None:
    labels = list(prior_labels)
    state: str | None = None
    state_reason: str | None = None
    for m in mutations:
        if m["kind"] == "add-label" and m["label"] not in labels:
            labels.append(m["label"])
        elif m["kind"] == "remove-label" and m["label"] in labels:
            labels.remove(m["label"])
        elif m["kind"] == "close":
            state, state_reason = "CLOSED", _MIRROR_STATE_REASON[m["reason"]]
        elif m["kind"] == "close-duplicate":
            state, state_reason = "CLOSED", "DUPLICATE"
    if state is None:
        con.execute(
            "UPDATE issues SET labels_json=? WHERE repo=? AND number=?",
            (json.dumps(labels), repo, number),
        )
    else:
        con.execute(
            "UPDATE issues SET labels_json=?, state=?, state_reason=?"
            " WHERE repo=? AND number=?",
            (json.dumps(labels), state, state_reason, repo, number),
        )
    con.commit()


def execute(
    con: sqlite3.Connection,
    *,
    decisions_dir: Any,
    proposals_dir: Any,
    results_dir: Any,
    run_gh: RunGh,
    apply: bool = False,
    auto: bool = False,
    autonomy_path: str = "config/autonomy.yaml",
    audit_rate: float = 0.10,
    repo: str | None = None,
    limit: int | None = None,
    labels_path: str = ".github/triage/labels.yaml",
    templates_dir: Any = templates_mod.DEFAULT_DIR,
    pace: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict:
    """Apply approved decisions to GitHub (dry-run unless apply=True)."""
    if auto and apply:
        promoted = load_autonomy(autonomy_path)
        all_props = review_queue.iter_jsonl_records(proposals_dir)
        decided = {
            d.get("proposal_id") for d in review_queue.iter_jsonl_records(decisions_dir)
        }
        auto_props = select_auto(all_props, decided, promoted, audit_rate=audit_rate)
        synthetic = []
        for p in auto_props:
            rec = {
                "id": uuid.uuid4().hex,
                "proposal_id": p["id"],
                "repo": p["repo"],
                "issue": p["issue"],
                "action": p["action"],
                "params": p["params"],
                "verdict": "auto-approved",
                "decided_by": "autonomy",
                "confidence": p.get("confidence"),
                "audit": p["audit"],
                "decided_at": _now(),
            }
            synthetic.append(rec)
        if synthetic:
            jsonl_log.append_weekly(synthetic, decisions_dir)

    tmpl = templates_mod.load(templates_dir)
    allowed = prompts.allowed_labels(labels_path)
    proposals_index = index_proposals(review_queue.iter_jsonl_records(proposals_dir))
    results = review_queue.iter_jsonl_records(results_dir)
    picked = select_executable(review_queue.iter_jsonl_records(decisions_dir), results)
    if repo is not None:
        picked = [d for d in picked if d["repo"] == repo]
    if limit is not None:
        picked = picked[:limit]

    batch_id = uuid.uuid4().hex
    counts = {"applied": 0, "dry-run": 0, "stale-needs-rereview": 0, "error": 0}
    first_mutation = True

    for decision in picked:
        rec = {
            "id": uuid.uuid4().hex,
            "batch_id": batch_id,
            "decision_id": decision["id"],
            "proposal_id": decision["proposal_id"],
            "repo": decision["repo"],
            "issue": decision["issue"],
            "action": decision["action"],
            "params": decision.get("params") or {},
            "executed_at": _now(),
        }
        proposal = proposals_index.get(decision["proposal_id"])
        if proposal is None:
            rec.update(status="error", error="proposal not found")
            jsonl_log.append_weekly([rec], results_dir)
            counts["error"] += 1
            continue
        try:
            issue = _fetch_issue(run_gh, decision["repo"], decision["issue"])
        except Exception as exc:  # gh.GhError or JSON decode
            rec.update(status="error", error=f"fetch failed: {exc}")
            jsonl_log.append_weekly([rec], results_dir)
            counts["error"] += 1
            continue
        rec["prior"] = _prior(issue)
        if issue["updated_at"] != proposal.get("issue_updated_at"):
            rec["status"] = "stale-needs-rereview"
            log(
                f"STALE {decision['repo']}#{decision['issue']}: updated_at moved "
                f"{proposal.get('issue_updated_at')} -> {issue['updated_at']}"
            )
            jsonl_log.append_weekly([rec], results_dir)
            counts["stale-needs-rereview"] += 1
            continue
        mutations, err = plan_decision(decision, issue, allowed=allowed, tmpl=tmpl)
        if err is not None:
            rec.update(status="error", error=err)
            log(f"ERROR {decision['repo']}#{decision['issue']}: {err}")
            jsonl_log.append_weekly([rec], results_dir)
            counts["error"] += 1
            continue
        header = f"{decision['repo']}#{decision['issue']} [{decision['action']}]"
        if not apply:
            for m in mutations:
                log(f"DRY-RUN {header}: {_describe(m)}")
            rec["status"] = "dry-run"
            jsonl_log.append_weekly([rec], results_dir)
            counts["dry-run"] += 1
            continue
        try:
            for m in mutations:
                if not first_mutation:
                    pace(1.0)
                first_mutation = False
                log(f"APPLY {header}: {_describe(m)}")
                comment_id = _apply_mutation(
                    run_gh,
                    decision["repo"],
                    decision["issue"],
                    issue["node_id"],
                    m,
                )
                if comment_id is not None:
                    rec["comment_id"] = comment_id
        except Exception as exc:
            rec.update(status="error", error=str(exc))
            jsonl_log.append_weekly([rec], results_dir)
            counts["error"] += 1
            continue
        try:
            _update_mirror(
                con,
                decision["repo"],
                decision["issue"],
                rec["prior"]["labels"],
                mutations,
            )
        except Exception as exc:
            log(f"WARN {header}: mirror update failed: {exc}")
        rec["status"] = "applied"
        jsonl_log.append_weekly([rec], results_dir)
        counts["applied"] += 1

    log(f"batch {batch_id}: {counts}")
    return {"batch_id": batch_id, "counts": counts}


def _reverse_mutations(rec: dict) -> list[dict]:
    """Mutations that reverse one applied result record."""
    action = rec["action"]
    params = rec.get("params") or {}
    prior_labels = rec.get("prior", {}).get("labels", [])
    muts: list[dict] = []
    if action == "add-label":
        label = params.get("label")
        if label and label not in prior_labels:
            muts.append({"kind": "remove-label", "label": label})
    elif action == "set-priority":
        label = f"Priority: {params.get('priority')}"
        if label not in prior_labels:
            muts.append({"kind": "remove-label", "label": label})
        muts.extend(
            {"kind": "add-label", "label": name}
            for name in prior_labels
            if name.startswith("Priority: ") and name != label
        )
    elif action in ("close", "close-duplicate"):
        if rec.get("comment_id") is not None:
            muts.append({"kind": "delete-comment", "comment_id": rec["comment_id"]})
        muts.append({"kind": "reopen"})
    return muts


def _apply_reverse(run_gh: RunGh, repo: str, number: int, mutation: dict) -> None:
    kind = mutation["kind"]
    if kind in ("add-label", "remove-label"):
        node_id = json.loads(run_gh(["api", f"repos/{repo}/issues/{number}"]))[
            "node_id"
        ]
        _apply_mutation(run_gh, repo, number, node_id, mutation)
    elif kind == "delete-comment":
        comment_node = _comment_node_id(run_gh, repo, mutation["comment_id"])
        gh_mod.gh_mutation(
            "deleteIssueComment", _DELETE_COMMENT, {"id": comment_node}, repos=[repo]
        )
    elif kind == "reopen":
        node_id = json.loads(run_gh(["api", f"repos/{repo}/issues/{number}"]))[
            "node_id"
        ]
        gh_mod.gh_mutation("reopenIssue", _REOPEN_ISSUE, {"id": node_id}, repos=[repo])


def _describe_reverse(mutation: dict) -> str:
    if mutation["kind"] == "delete-comment":
        return f"delete comment {mutation['comment_id']}"
    if mutation["kind"] == "reopen":
        return "reopen"
    return _describe(mutation)


def _undo_mirror(con: sqlite3.Connection, rec: dict) -> None:
    prior = rec.get("prior")
    if not prior:
        return
    con.execute(
        "UPDATE issues SET labels_json=?, state=?, state_reason=?"
        " WHERE repo=? AND number=?",
        (
            json.dumps(prior["labels"]),
            prior["state"].upper(),
            prior["state_reason"].upper() if prior.get("state_reason") else None,
            rec["repo"],
            rec["issue"],
        ),
    )
    con.commit()


def undo(
    con: sqlite3.Connection,
    *,
    results_dir: Any,
    batch_id: str,
    run_gh: RunGh,
    issue: str | None = None,
    apply: bool = False,
    pace: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict:
    """Reverse an executed batch (dry-run unless apply=True)."""
    all_results = review_queue.iter_jsonl_records(results_dir)
    already_undone = {
        r["undoes_result_id"]
        for r in all_results
        if r.get("action") == "undo" and r.get("status") == "applied"
    }
    targets = [
        r
        for r in all_results
        if r.get("batch_id") == batch_id
        and r.get("status") == "applied"
        and r.get("action") != "undo"
    ]
    if issue is not None:
        ref = parse_issue_ref(issue, default_repo="")
        if ref is None:
            raise ValueError(f"cannot parse --issue value: {issue!r}")
        targets = [r for r in targets if (r["repo"], r["issue"]) == ref]

    undo_batch_id = uuid.uuid4().hex
    counts = {"applied": 0, "dry-run": 0, "error": 0, "skipped": 0}
    first_mutation = True

    for rec in reversed(targets):
        header = f"{rec['repo']}#{rec['issue']} [undo {rec['action']}]"
        if rec["id"] in already_undone:
            log(f"SKIP {header}: already undone")
            counts["skipped"] += 1
            continue
        out = {
            "id": uuid.uuid4().hex,
            "batch_id": undo_batch_id,
            "undoes_result_id": rec["id"],
            "action": "undo",
            "repo": rec["repo"],
            "issue": rec["issue"],
            "params": {"undone_action": rec["action"]},
            "executed_at": _now(),
        }
        mutations = _reverse_mutations(rec)
        if not apply:
            for m in mutations:
                log(f"DRY-RUN {header}: {_describe_reverse(m)}")
            out["status"] = "dry-run"
            jsonl_log.append_weekly([out], results_dir)
            counts["dry-run"] += 1
            continue
        try:
            for m in mutations:
                if not first_mutation:
                    pace(1.0)
                first_mutation = False
                log(f"APPLY {header}: {_describe_reverse(m)}")
                _apply_reverse(run_gh, rec["repo"], rec["issue"], m)
        except Exception as exc:
            out.update(status="error", error=str(exc))
            jsonl_log.append_weekly([out], results_dir)
            counts["error"] += 1
            continue
        try:
            _undo_mirror(con, rec)
        except Exception as exc:
            log(f"WARN {header}: mirror update failed: {exc}")
        out["status"] = "applied"
        jsonl_log.append_weekly([out], results_dir)
        counts["applied"] += 1

    log(f"undo batch {undo_batch_id} (undoes {batch_id}): {counts}")
    return {"batch_id": undo_batch_id, "counts": counts}
