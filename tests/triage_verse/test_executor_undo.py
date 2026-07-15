"""Round-trip tests for executor.undo."""

import importlib.util
import pathlib

from triage_verse import db, decisions, executor, jsonl_log

_spec = importlib.util.spec_from_file_location(
    "fake_gh", pathlib.Path(__file__).parent / "fake_gh.py"
)
_fake_gh_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake_gh_module)
FakeGh = _fake_gh_module.FakeGh

UPDATED = "2026-07-01T00:00:00Z"


def _proposal(pid, action, params, issue=1):
    return {
        "id": pid,
        "repo": "o/r",
        "issue": issue,
        "issue_updated_at": UPDATED,
        "run_id": "run1",
        "model": "m",
        "confidence": 0.9,
        "evidence": [],
        "action": action,
        "params": params,
        "rationale": "",
    }


def _issue(labels=(), node="N1"):
    return {
        "labels": list(labels),
        "state": "open",
        "state_reason": None,
        "updated_at": UPDATED,
        "node_id": node,
    }


def _run_batch(tmp_path, proposal_records, gh):
    dirs = {
        "decisions_dir": tmp_path / "decisions",
        "proposals_dir": tmp_path / "proposals",
        "results_dir": tmp_path / "results",
    }
    jsonl_log.append_weekly(proposal_records, dirs["proposals_dir"])
    jsonl_log.append_weekly(
        [decisions.record(p, "approved") for p in proposal_records],
        dirs["decisions_dir"],
    )
    con = db.connect(":memory:")
    for p in proposal_records:
        con.execute(
            "INSERT OR IGNORE INTO issues (repo, number, title, state, updated_at,"
            " created_at, labels_json) VALUES (?,?,?,?,?,?,?)",
            (p["repo"], p["issue"], "t", "OPEN", UPDATED, UPDATED, "[]"),
        )
    summary = executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    return con, dirs, summary["batch_id"]


def test_undo_round_trip_restores_labels_state_and_comments(tmp_path):
    gh = FakeGh(
        {
            ("o/r", 1): _issue(labels=["Priority: Low", "bug"]),
            ("o/r", 2): _issue(node="N2"),
        }
    )
    con, dirs, batch_id = _run_batch(
        tmp_path,
        [
            _proposal("p1", "set-priority", {"priority": "High"}),
            _proposal("p2", "close", {"reason": "fixed"}, issue=2),
        ],
        gh,
    )
    assert gh.issues[("o/r", 1)]["labels"] == ["bug", "Priority: High"]
    assert gh.issues[("o/r", 2)]["state"] == "closed"
    assert len(gh.comments) == 1

    summary = executor.undo(
        con,
        results_dir=dirs["results_dir"],
        batch_id=batch_id,
        run_gh=gh,
        apply=True,
        pace=lambda s: None,
        log=lambda *a: None,
    )
    assert summary["counts"]["applied"] == 2
    assert sorted(gh.issues[("o/r", 1)]["labels"]) == ["Priority: Low", "bug"]
    assert gh.issues[("o/r", 2)]["state"] == "open"
    assert gh.comments == {}
    row = db.get_issue(con, "o/r", 2)
    assert row["state"] == "OPEN" and row["state_reason"] is None


def test_undo_dry_run_by_default(tmp_path):
    gh = FakeGh({("o/r", 1): _issue()})
    con, dirs, batch_id = _run_batch(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], gh
    )
    before = len(gh.mutating_calls)
    summary = executor.undo(
        con,
        results_dir=dirs["results_dir"],
        batch_id=batch_id,
        run_gh=gh,
        pace=lambda s: None,
        log=lambda *a: None,
    )
    assert summary["counts"]["dry-run"] == 1
    assert len(gh.mutating_calls) == before
    assert gh.issues[("o/r", 1)]["labels"] == ["regression"]


def test_undo_is_idempotent(tmp_path):
    gh = FakeGh({("o/r", 1): _issue()})
    con, dirs, batch_id = _run_batch(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], gh
    )
    executor.undo(
        con,
        results_dir=dirs["results_dir"],
        batch_id=batch_id,
        run_gh=gh,
        apply=True,
        pace=lambda s: None,
        log=lambda *a: None,
    )
    summary = executor.undo(
        con,
        results_dir=dirs["results_dir"],
        batch_id=batch_id,
        run_gh=gh,
        apply=True,
        pace=lambda s: None,
        log=lambda *a: None,
    )
    assert summary["counts"]["applied"] == 0
    assert summary["counts"]["skipped"] == 1


def test_undo_does_not_remove_preexisting_label(tmp_path):
    # add-label on an issue that already carried the label: undo must not strip it.
    gh = FakeGh({("o/r", 1): _issue(labels=["regression"])})
    con, dirs, batch_id = _run_batch(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], gh
    )
    executor.undo(
        con,
        results_dir=dirs["results_dir"],
        batch_id=batch_id,
        run_gh=gh,
        apply=True,
        pace=lambda s: None,
        log=lambda *a: None,
    )
    assert gh.issues[("o/r", 1)]["labels"] == ["regression"]


def test_undo_issue_filter(tmp_path):
    gh = FakeGh({("o/r", 1): _issue(), ("o/r", 2): _issue(node="N2")})
    con, dirs, batch_id = _run_batch(
        tmp_path,
        [
            _proposal("p1", "add-label", {"label": "regression"}),
            _proposal("p2", "add-label", {"label": "duplicate"}, issue=2),
        ],
        gh,
    )
    executor.undo(
        con,
        results_dir=dirs["results_dir"],
        batch_id=batch_id,
        run_gh=gh,
        issue="o/r#1",
        apply=True,
        pace=lambda s: None,
        log=lambda *a: None,
    )
    assert gh.issues[("o/r", 1)]["labels"] == []
    assert gh.issues[("o/r", 2)]["labels"] == ["duplicate"]
