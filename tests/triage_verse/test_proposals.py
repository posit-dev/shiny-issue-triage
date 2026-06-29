# tests/triage_verse/test_proposals.py
import json

from triage_verse import db, proposals


def _seed(con):
    con.execute(
        "INSERT INTO issues (repo, number, title, state, created_at,"
        " updated_at, is_pr) VALUES ('r/r', 1, 'T', 'OPEN',"
        " '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z', 0)"
    )
    db.upsert_classification(
        con,
        {
            "repo": "r/r",
            "number": 1,
            "clf_hash": "h",
            "type": "fix",
            "priority": "High",
            "assessment": "actionable",
            "labels_json": json.dumps(["needs reprex"]),
            "close_candidate_json": json.dumps(
                {"reason": "fixed", "rationale": "v1.2", "confidence": 0.95}
            ),
            "confidence": 0.95,
            "model": "claude-sonnet-4-6",
            "run_id": "run1",
            "at": "2026-06-29T00:00:00Z",
        },
    )
    con.commit()


def test_build_emits_label_priority_and_close(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)
    recs = proposals.build(con, "run1")
    actions = {r["action"] for r in recs}
    assert {"add-label", "set-priority", "close"} <= actions
    close = next(r for r in recs if r["action"] == "close")
    assert close["issue_updated_at"] == "2026-06-01T00:00:00Z"
    assert close["params"]["reason"] == "fixed"


def test_write_appends_weekly_partition(tmp_path):
    recs = [
        {
            "id": "x",
            "repo": "r/r",
            "issue": 1,
            "action": "add-label",
            "params": {"label": "needs reprex"},
            "rationale": "",
            "confidence": 0.9,
            "evidence": [],
            "issue_updated_at": "2026-06-01T00:00:00Z",
            "run_id": "run1",
            "model": "claude-haiku-4-5",
        }
    ]
    path = proposals.write(recs, tmp_path / "proposals", today="2026-06-29")
    assert path.exists()
    assert "2026/W27.jsonl" in str(path).replace("\\", "/")
    line = json.loads(path.read_text().splitlines()[0])
    assert line["action"] == "add-label"
    # appends, not overwrites
    proposals.write(recs, tmp_path / "proposals", today="2026-06-29")
    assert len(path.read_text().splitlines()) == 2
