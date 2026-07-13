"""Tests for executor decision selection."""

from triage_verse import executor


def _decision(pid, verdict, decided_at, did=None):
    return {
        "id": did or f"d-{pid}-{decided_at}",
        "proposal_id": pid,
        "repo": "o/r",
        "issue": 1,
        "action": "add-label",
        "params": {"label": "regression"},
        "verdict": verdict,
        "decided_at": decided_at,
    }


def test_keeps_only_approving_verdicts():
    decisions = [
        _decision("p1", "approved", "2026-07-13T00:00:00Z"),
        _decision("p2", "rejected", "2026-07-13T00:00:00Z"),
        _decision("p3", "skipped", "2026-07-13T00:00:00Z"),
        _decision("p4", "edited", "2026-07-13T00:00:00Z"),
    ]
    picked = executor.select_executable(decisions, [])
    assert {d["proposal_id"] for d in picked} == {"p1", "p4"}


def test_latest_decision_per_proposal_wins():
    decisions = [
        _decision("p1", "approved", "2026-07-12T00:00:00Z"),
        _decision("p1", "rejected", "2026-07-13T00:00:00Z"),
        _decision("p2", "rejected", "2026-07-12T00:00:00Z"),
        _decision("p2", "approved", "2026-07-13T00:00:00Z"),
    ]
    picked = executor.select_executable(decisions, [])
    assert {d["proposal_id"] for d in picked} == {"p2"}


def test_finalized_results_block_reexecution_but_dry_run_does_not():
    d1 = _decision("p1", "approved", "2026-07-13T00:00:00Z", did="d1")
    d2 = _decision("p2", "approved", "2026-07-13T00:00:00Z", did="d2")
    d3 = _decision("p3", "approved", "2026-07-13T00:00:00Z", did="d3")
    d4 = _decision("p4", "approved", "2026-07-13T00:00:00Z", did="d4")
    results = [
        {"decision_id": "d1", "status": "applied"},
        {"decision_id": "d2", "status": "dry-run"},
        {"decision_id": "d3", "status": "stale-needs-rereview"},
        {"decision_id": "d4", "status": "error"},
    ]
    picked = executor.select_executable([d1, d2, d3, d4], results)
    assert [d["id"] for d in picked] == ["d2"]


def test_index_proposals():
    p = {"id": "p1", "action": "close"}
    assert executor.index_proposals([p]) == {"p1": p}
