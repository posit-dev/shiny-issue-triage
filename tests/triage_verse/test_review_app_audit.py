# tests/triage_verse/test_review_app_audit.py
"""Audit-section helper: list executed auto-decisions flagged for audit."""

from triage_verse import jsonl_log
from triage_verse.review_app import app as review_app


def test_app_audit_items_lists_executed_audit_flagged(tmp_path):
    dec = tmp_path / "decisions"
    res = tmp_path / "results"
    jsonl_log.append_weekly(
        [
            {
                "id": "d1",
                "proposal_id": "p1",
                "repo": "o/r",
                "issue": 1,
                "action": "add-label",
                "params": {"label": "regression"},
                "verdict": "auto-approved",
                "decided_by": "autonomy",
                "audit": True,
            },
            {
                "id": "d2",
                "proposal_id": "p2",
                "repo": "o/r",
                "issue": 2,
                "action": "add-label",
                "params": {"label": "regression"},
                "verdict": "auto-approved",
                "decided_by": "autonomy",
                "audit": False,
            },
        ],
        dec,
    )
    jsonl_log.append_weekly(
        [
            {
                "id": "r1",
                "decision_id": "d1",
                "batch_id": "b1",
                "repo": "o/r",
                "issue": 1,
                "action": "add-label",
                "status": "applied",
            },
            {
                "id": "r2",
                "decision_id": "d2",
                "batch_id": "b1",
                "repo": "o/r",
                "issue": 2,
                "action": "add-label",
                "status": "applied",
            },
        ],
        res,
    )
    items = review_app.app_audit_items(dec, res)
    assert len(items) == 1
    assert items[0]["issue"] == 1 and items[0]["batch_id"] == "b1"
    assert items[0]["proposal_id"] == "p1"


def test_app_audit_reject_records_rejected_decision(tmp_path):
    dec = tmp_path / "decisions"
    item = {
        "repo": "o/r",
        "issue": 5,
        "action": "add-label",
        "params": {"label": "bug"},
        "batch_id": "batch99",
        "result_id": "r5",
        "proposal_id": "p5",
    }
    result = review_app.app_audit_reject(item, decisions_dir=str(dec))
    assert "undo --batch" in result
    assert "batch99" in result
    assert "o/r#5" in result

    # Read back the written decision
    from triage_verse import review_queue

    records = list(review_queue.iter_jsonl_records(str(dec)))
    assert len(records) == 1
    rec = records[0]
    assert rec["verdict"] == "rejected"
    assert rec["proposal_id"] == "p5"
    # Must NOT be decided_by=autonomy (so it counts as human precision failure)
    assert rec.get("decided_by") != "autonomy"
