"""execute --auto: synthetic decisions + deterministic audit sampling."""

import importlib.util
import pathlib

from triage_verse import db, executor, jsonl_log, review_queue

_spec = importlib.util.spec_from_file_location(
    "fake_gh", pathlib.Path(__file__).with_name("fake_gh.py")
)
fake_gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_gh)
FakeGh = fake_gh.FakeGh

UPDATED = "2026-07-01T00:00:00Z"


def _proposal(pid, action, params, conf, issue=1):
    return {
        "id": pid,
        "repo": "o/r",
        "issue": issue,
        "issue_updated_at": UPDATED,
        "run_id": "r",
        "model": "m",
        "confidence": conf,
        "evidence": [],
        "action": action,
        "params": params,
        "rationale": "",
    }


def test_select_auto_filters_by_promotion_and_floor():
    proposals = [
        _proposal("p1", "add-label", {"label": "regression"}, 0.95),
        _proposal("p2", "add-label", {"label": "regression"}, 0.5),  # below floor
        _proposal("p3", "close", {"reason": "fixed"}, 0.99, issue=2),  # not eligible
    ]
    promoted = {"add-label": {"confidence_floor": 0.9}}
    picked = executor.select_auto(proposals, set(), promoted, audit_rate=0.0)
    assert [p["id"] for p in picked] == ["p1"]
    assert picked[0]["audit"] is False


def test_auto_writes_synthetic_decisions_then_executes(tmp_path, gh_relay):
    dirs = {
        "decisions_dir": tmp_path / "dec",
        "proposals_dir": tmp_path / "prop",
        "results_dir": tmp_path / "res",
    }
    jsonl_log.append_weekly(
        [_proposal("p1", "add-label", {"label": "regression"}, 0.95)],
        dirs["proposals_dir"],
    )
    autonomy_path = tmp_path / "autonomy.yaml"
    autonomy_path.write_text(
        "promoted:\n  add-label: {confidence_floor: 0.9}\n", encoding="utf-8"
    )
    con = db.connect(":memory:")
    con.execute(
        "INSERT INTO issues (repo, number, title, state, updated_at, created_at,"
        " labels_json) VALUES ('o/r',1,'t','OPEN',?,?, '[]')",
        (UPDATED, UPDATED),
    )
    gh = FakeGh(
        {
            ("o/r", 1): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N1",
            }
        }
    )
    gh_relay.install(gh)
    summary = executor.execute(
        con,
        run_gh=gh,
        apply=True,
        auto=True,
        autonomy_path=str(autonomy_path),
        pace=lambda s: None,
        log=lambda *a: None,
        **dirs,
    )
    assert summary["counts"]["applied"] == 1
    dec = review_queue.iter_jsonl_records(dirs["decisions_dir"])
    assert dec[0]["verdict"] == "auto-approved" and dec[0]["decided_by"] == "autonomy"
    assert gh.issues[("o/r", 1)]["labels"] == ["regression"]


def test_audit_sampling_is_deterministic():
    proposals = [
        _proposal(f"p{i}", "add-label", {"label": "regression"}, 0.95)
        for i in range(100)
    ]
    promoted = {"add-label": {"confidence_floor": 0.9}}
    a = executor.select_auto(proposals, set(), promoted, audit_rate=0.10)
    b = executor.select_auto(proposals, set(), promoted, audit_rate=0.10)
    assert [p["audit"] for p in a] == [p["audit"] for p in b]
    flagged = sum(1 for p in a if p["audit"])
    assert 3 <= flagged <= 20  # ~10% of 100, deterministic band
