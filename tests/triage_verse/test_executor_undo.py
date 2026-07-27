"""Round-trip tests for executor.undo."""

import importlib.util
import json
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
        [decisions.record(p, "approved", decided_by="alice") for p in proposal_records],
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


def test_undo_round_trip_restores_labels_state_and_comments(tmp_path, gh_relay):
    gh = FakeGh(
        {
            ("o/r", 1): _issue(labels=["Priority: Low", "bug"]),
            ("o/r", 2): _issue(node="N2"),
        }
    )
    gh_relay.install(gh)
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


def test_undo_dry_run_by_default(tmp_path, gh_relay):
    gh = FakeGh({("o/r", 1): _issue()})
    gh_relay.install(gh)
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


def test_undo_is_idempotent(tmp_path, gh_relay):
    gh = FakeGh({("o/r", 1): _issue()})
    gh_relay.install(gh)
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


def test_undo_does_not_remove_preexisting_label(tmp_path, gh_relay):
    # add-label on an issue that already carried the label: undo must not strip it.
    gh = FakeGh({("o/r", 1): _issue(labels=["regression"])})
    gh_relay.install(gh)
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


def test_undo_link_duplicate_deletes_the_comment_and_does_not_reopen():
    """link-duplicate closed nothing, so its reversal is the comment alone."""
    rec = {
        "action": "link-duplicate",
        "params": {"canonical": "o/other#3"},
        "prior": {"labels": [], "state": "open", "state_reason": None},
        "comment_id": 555,
    }

    muts = executor._reverse_mutations(rec)

    assert muts == [{"kind": "delete-comment", "comment_id": 555}]
    assert not any(m["kind"] == "reopen" for m in muts)


def test_undo_suggest_transfer_removes_the_transfer_label():
    rec = {
        "action": "suggest-transfer",
        "params": {"canonical": "o/other#3"},
        "prior": {"labels": ["bug"], "state": "open", "state_reason": None},
    }

    muts = executor._reverse_mutations(rec)

    assert muts == [{"kind": "remove-label", "label": executor.TRANSFER_LABEL}]


def test_undo_suggest_transfer_keeps_a_preexisting_transfer_label():
    rec = {
        "action": "suggest-transfer",
        "params": {"canonical": "o/other#3"},
        "prior": {
            "labels": [executor.TRANSFER_LABEL],
            "state": "open",
            "state_reason": None,
        },
    }

    assert executor._reverse_mutations(rec) == []


def test_undo_of_an_action_without_a_rule_is_not_reported_as_applied(
    tmp_path, gh_relay
):
    """An action with no reversal rule must not look like a successful undo.

    `_reverse_mutations` returns None for it (distinct from the empty list an
    existing rule may legitimately compute), so undo records `not-reversible`
    instead of `applied` -- which also leaves the record retryable once a rule
    exists, since `already_undone` only counts applied undos.
    """
    gh = FakeGh({("o/r", 1): _issue()})
    gh_relay.install(gh)
    con, dirs, batch_id = _run_batch(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], gh
    )
    # Rewrite the applied result to an action the reverser knows nothing about.
    path = next((dirs["results_dir"]).glob("**/*.jsonl"))
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        rec["action"] = "make-coffee"
        lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = len(gh.mutating_calls)

    summary = executor.undo(
        con,
        results_dir=dirs["results_dir"],
        batch_id=batch_id,
        run_gh=gh,
        apply=True,
        pace=lambda s: None,
        log=lambda *a: None,
    )

    assert summary["counts"]["not-reversible"] == 1
    assert summary["counts"]["applied"] == 0
    assert len(gh.mutating_calls) == before
    statuses = [
        json.loads(line)["status"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("action") == "undo"
    ]
    assert statuses == ["not-reversible"]


def test_undo_of_an_already_present_label_is_still_a_success(tmp_path, gh_relay):
    """An empty reversal from a rule that ran is 'nothing to do', not an error."""
    gh = FakeGh({("o/r", 1): _issue(labels=["regression"])})
    gh_relay.install(gh)
    con, dirs, batch_id = _run_batch(
        tmp_path, [_proposal("p1", "add-label", {"label": "regression"})], gh
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

    assert summary["counts"]["applied"] == 1
    assert summary["counts"]["not-reversible"] == 0


def test_undo_issue_filter(tmp_path, gh_relay):
    gh = FakeGh({("o/r", 1): _issue(), ("o/r", 2): _issue(node="N2")})
    gh_relay.install(gh)
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
