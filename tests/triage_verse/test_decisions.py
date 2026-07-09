import json

from triage_verse import decisions


def _proposal(**overrides):
    row = {
        "id": "p1",
        "repo": "r/r",
        "issue": 1,
        "action": "add-label",
        "params": {"label": "bug"},
        "confidence": 0.42,
    }
    row.update(overrides)
    return row


def test_record_copies_proposal_fields():
    rec = decisions.record(_proposal(), "approved")
    assert rec["proposal_id"] == "p1"
    assert rec["repo"] == "r/r"
    assert rec["issue"] == 1
    assert rec["action"] == "add-label"
    assert rec["params"] == {"label": "bug"}
    assert rec["verdict"] == "approved"
    assert rec["confidence"] == 0.42
    assert rec["id"] != "p1"
    assert rec["decided_at"].endswith("Z")


def test_write_appends_weekly_partition(tmp_path):
    rec = decisions.record(_proposal(), "rejected")
    path = decisions.write([rec], tmp_path / "decisions", today="2026-06-29")
    assert path.exists()
    assert "2026/W27.jsonl" in str(path).replace("\\", "/")
    line = json.loads(path.read_text().splitlines()[0])
    assert line["verdict"] == "rejected"
    # appends, not overwrites
    decisions.write([rec], tmp_path / "decisions", today="2026-06-29")
    assert len(path.read_text().splitlines()) == 2
