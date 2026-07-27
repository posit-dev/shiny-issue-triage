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
    rec = decisions.record(_proposal(), "approved", decided_by="alice")
    assert rec["proposal_id"] == "p1"
    assert rec["repo"] == "r/r"
    assert rec["issue"] == 1
    assert rec["action"] == "add-label"
    assert rec["params"] == {"label": "bug"}
    assert rec["verdict"] == "approved"
    assert rec["confidence"] == 0.42
    assert rec["id"] != "p1"
    assert rec["decided_at"].endswith("Z")


def test_record_edited_params_override():
    rec = decisions.record(
        _proposal(), "edited", params={"label": "good first issue"}, decided_by="alice"
    )
    assert rec["verdict"] == "edited"
    assert rec["params"] == {"label": "good first issue"}
    assert rec["proposed_params"] == {"label": "bug"}


def test_record_without_override_has_no_proposed_params():
    rec = decisions.record(_proposal(), "approved", decided_by="alice")
    assert "proposed_params" not in rec


def test_record_stamps_decided_by():
    rec = decisions.record(_proposal(), "approved", decided_by="alice")
    assert rec["decided_by"] == "alice"


def test_record_omits_empty_reason_includes_nonempty():
    no_reason = decisions.record(_proposal(), "approved", decided_by="alice")
    assert "reason" not in no_reason

    blank = decisions.record(_proposal(), "approved", decided_by="alice", reason="")
    assert "reason" not in blank

    with_reason = decisions.record(
        _proposal(), "rejected", decided_by="alice", reason="wrong label"
    )
    assert with_reason["reason"] == "wrong label"


def test_write_appends_weekly_partition(tmp_path):
    rec = decisions.record(_proposal(), "rejected", decided_by="alice")
    path = decisions.write([rec], tmp_path / "decisions", today="2026-06-29")
    assert path.exists()
    assert "2026/W27.jsonl" in str(path).replace("\\", "/")
    line = json.loads(path.read_text().splitlines()[0])
    assert line["verdict"] == "rejected"
    # appends, not overwrites
    decisions.write([rec], tmp_path / "decisions", today="2026-06-29")
    assert len(path.read_text().splitlines()) == 2
