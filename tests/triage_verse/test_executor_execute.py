"""End-to-end tests for executor.execute against a stateful fake gh."""

import importlib.util
import json
import pathlib

from triage_verse import db, decisions, executor, jsonl_log, review_queue

_spec = importlib.util.spec_from_file_location(
    "fake_gh", pathlib.Path(__file__).parent / "fake_gh.py"
)
_fake_gh_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fake_gh_module)
FakeGh = _fake_gh_module.FakeGh

LABELS_PATH = ".github/triage/labels.yaml"
UPDATED = "2026-07-01T00:00:00Z"


def _proposal(pid, action, params, issue=1, confidence=0.95):
    return {
        "id": pid,
        "repo": "o/r",
        "issue": issue,
        "issue_updated_at": UPDATED,
        "run_id": "run1",
        "model": "m",
        "confidence": confidence,
        "evidence": [],
        "action": action,
        "params": params,
        "rationale": "model text that must never be posted",
    }


def _setup(tmp_path, proposal_records, verdicts):
    dirs = {
        "decisions_dir": tmp_path / "decisions",
        "proposals_dir": tmp_path / "proposals",
        "results_dir": tmp_path / "results",
    }
    jsonl_log.append_weekly(proposal_records, dirs["proposals_dir"])
    decision_records = [
        decisions.record(p, verdict) for p, verdict in zip(proposal_records, verdicts)
    ]
    jsonl_log.append_weekly(decision_records, dirs["decisions_dir"])
    con = db.connect(":memory:")
    for p in proposal_records:
        con.execute(
            "INSERT OR IGNORE INTO issues (repo, number, title, state, updated_at,"
            " created_at, labels_json) VALUES (?,?,?,?,?,?,?)",
            (p["repo"], p["issue"], "t", "OPEN", UPDATED, UPDATED, "[]"),
        )
    return con, dirs


def _fake(issues=None):
    base = {
        "labels": [],
        "state": "open",
        "state_reason": None,
        "updated_at": UPDATED,
        "node_id": "N1",
    }
    return FakeGh(issues or {("o/r", 1): base})


def test_dry_run_writes_records_and_never_mutates(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], ["approved"]
    )
    gh = _fake()
    lines = []
    summary = executor.execute(
        con, run_gh=gh, apply=False, pace=lambda s: None, log=lines.append, **dirs
    )
    assert summary["counts"] == {
        "applied": 0,
        "dry-run": 1,
        "stale-needs-rereview": 0,
        "error": 0,
    }
    assert gh.mutating_calls == []
    [rec] = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert rec["status"] == "dry-run"
    assert rec["batch_id"] == summary["batch_id"]
    assert rec["prior"] == {"labels": [], "state": "open", "state_reason": None}
    assert any("add-label" in line for line in lines)


def test_apply_add_label_updates_github_results_and_mirror(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], ["approved"]
    )
    gh = _fake()
    executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    assert gh.issues[("o/r", 1)]["labels"] == ["regression"]
    [rec] = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert rec["status"] == "applied"
    row = db.get_issue(con, "o/r", 1)
    assert json.loads(row["labels_json"]) == ["regression"]


def test_apply_close_posts_template_comment_then_closes(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "close", {"reason": "fixed"})], ["approved"]
    )
    gh = _fake()
    executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    issue = gh.issues[("o/r", 1)]
    assert issue["state"] == "closed" and issue["state_reason"] == "completed"
    [comment] = gh.comments.values()
    assert "reopen" in comment["body"]
    assert "model text" not in comment["body"]  # rationale never posted
    [rec] = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert rec["comment_id"] in gh.comments
    row = db.get_issue(con, "o/r", 1)
    assert row["state"] == "CLOSED" and row["state_reason"] == "COMPLETED"


def test_apply_close_duplicate_uses_graphql_duplicate_close(tmp_path):
    con, dirs = _setup(
        tmp_path,
        [
            _proposal(
                "p1",
                "close-duplicate",
                {"canonical": "o/r#2", "cross_repo_option": None},
            )
        ],
        ["approved"],
    )
    gh = _fake(
        {
            ("o/r", 1): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N1",
            },
            ("o/r", 2): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N2",
            },
        }
    )
    executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    issue = gh.issues[("o/r", 1)]
    assert issue["state"] == "closed" and issue["state_reason"] == "duplicate"
    row = db.get_issue(con, "o/r", 1)
    assert row["state"] == "CLOSED" and row["state_reason"] == "DUPLICATE"


def test_stale_issue_bounces_without_mutation(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "close", {"reason": "fixed"})], ["approved"]
    )
    gh = _fake(
        {
            ("o/r", 1): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": "2026-07-12T09:00:00Z",
                "node_id": "N1",
            }
        }
    )
    summary = executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    assert summary["counts"]["stale-needs-rereview"] == 1
    assert gh.mutating_calls == []
    [rec] = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert rec["status"] == "stale-needs-rereview"


def test_error_records_continue_the_batch(tmp_path):
    con, dirs = _setup(
        tmp_path,
        [
            _proposal("p1", "add-label", {"label": "evil"}),
            _proposal("p2", "add-label", {"label": "regression"}, issue=2),
        ],
        ["approved", "approved"],
    )
    gh = _fake(
        {
            ("o/r", 1): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N1",
            },
            ("o/r", 2): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N2",
            },
        }
    )
    summary = executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    assert summary["counts"]["error"] == 1
    assert summary["counts"]["applied"] == 1
    assert gh.issues[("o/r", 2)]["labels"] == ["regression"]


def test_rerun_after_apply_skips_finalized_decisions(tmp_path):
    con, dirs = _setup(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], ["approved"]
    )
    gh = _fake()
    executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    first_mutations = len(gh.mutating_calls)
    summary = executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    assert len(gh.mutating_calls) == first_mutations
    assert summary["counts"] == {
        "applied": 0,
        "dry-run": 0,
        "stale-needs-rereview": 0,
        "error": 0,
    }


def test_records_are_written_incrementally_per_decision(tmp_path, monkeypatch):
    con, dirs = _setup(
        tmp_path,
        [
            _proposal("p1", "add-label", {"label": "regression"}),
            _proposal("p2", "add-label", {"label": "duplicate"}, issue=2),
        ],
        ["approved", "approved"],
    )
    gh = _fake(
        {
            ("o/r", 1): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N1",
            },
            ("o/r", 2): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N2",
            },
        }
    )
    real_append_weekly = jsonl_log.append_weekly
    calls = []

    def counting_append_weekly(records, results_dir):
        calls.append(records)
        return real_append_weekly(records, results_dir)

    monkeypatch.setattr(executor.jsonl_log, "append_weekly", counting_append_weekly)
    summary = executor.execute(
        con, run_gh=gh, apply=True, pace=lambda s: None, log=lambda *a: None, **dirs
    )
    assert summary["counts"]["applied"] == 2
    assert len(calls) == 2
    assert all(len(c) == 1 for c in calls)
    results = review_queue.iter_jsonl_records(dirs["results_dir"])
    assert len(results) == 2


def test_repo_filter_and_limit(tmp_path):
    con, dirs = _setup(
        tmp_path,
        [
            _proposal("p1", "add-label", {"label": "regression"}),
            _proposal("p2", "add-label", {"label": "duplicate"}, issue=2),
        ],
        ["approved", "approved"],
    )
    gh = _fake(
        {
            ("o/r", 1): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N1",
            },
            ("o/r", 2): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": UPDATED,
                "node_id": "N2",
            },
        }
    )
    summary = executor.execute(
        con,
        run_gh=gh,
        apply=False,
        repo="other/repo",
        pace=lambda s: None,
        log=lambda *a: None,
        **dirs,
    )
    assert sum(summary["counts"].values()) == 0
    summary = executor.execute(
        con,
        run_gh=gh,
        apply=False,
        limit=1,
        pace=lambda s: None,
        log=lambda *a: None,
        **dirs,
    )
    assert sum(summary["counts"].values()) == 1
