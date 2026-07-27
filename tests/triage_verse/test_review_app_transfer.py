"""Rendering of link-duplicate and suggest-transfer in the review app."""

import json

from triage_verse import review_queue
from triage_verse.review_app import app


def _proposal(action, canonical="posit-dev/py-shiny#12"):
    return {
        "id": "p1",
        "repo": "rstudio/shiny",
        "issue": 7,
        "action": action,
        "params": {"canonical": canonical, "cross_repo_option": "transfer"},
        "confidence": 0.9,
        "rationale": "belongs in the python package",
        "evidence": [],
    }


def test_params_line_for_suggest_transfer_shows_destination():
    line = app._params_line(_proposal("suggest-transfer"))
    assert "posit-dev/py-shiny" in line
    assert "canonical" not in line


def test_params_line_for_link_duplicate_reads_as_words():
    line = app._params_line(_proposal("link-duplicate"))
    assert "posit-dev/py-shiny#12" in line
    assert not line.startswith("{")


def test_params_line_falls_back_to_params_for_other_actions():
    p = {"action": "add-label", "params": {"label": "regression"}}
    assert "regression" in app._params_line(p)


def test_row_label_uses_the_readable_params_line():
    label = app._row_label(_proposal("suggest-transfer"))
    assert label.startswith("rstudio/shiny#7 — suggest-transfer:")
    assert "posit-dev/py-shiny" in label
    assert "cross_repo_option" not in label


def test_drawer_transfer_states_that_approving_does_not_move_the_issue():
    parts = app._drawer_transfer(_proposal("suggest-transfer"))
    text = " ".join(str(p) for p in parts)
    assert "posit-dev/py-shiny" in text
    assert "wrong location" in text
    assert "Transfers" in text


def test_drawer_transfer_handles_a_missing_destination():
    parts = app._drawer_transfer(_proposal("suggest-transfer", canonical=None))
    text = " ".join(str(p) for p in parts)
    assert "not identified" in text


def test_destination_badge_renders_leftmost_of_the_other_badges():
    """The destination badge must precede the stale and not-now badges.

    The relative order of `stale` and `not now` between themselves is
    pre-existing behaviour of those two blocks and is not asserted here.
    """
    p = _proposal("suggest-transfer")
    p["stale"] = True
    p["deferred"] = True
    html = str(app.row_ui("p1", p, "snippet"))
    i_dest = html.index("posit-dev/py-shiny")
    assert i_dest < html.index("stale")
    assert i_dest < html.index("not now")


def _approved(pid="p1"):
    return {
        "id": f"d-{pid}",
        "proposal_id": pid,
        "repo": "rstudio/shiny",
        "issue": 7,
        "action": "suggest-transfer",
        "params": {
            "canonical": "posit-dev/py-shiny#12",
            "cross_repo_option": "transfer",
        },
        "verdict": "approved",
        "confidence": 0.9,
        "decided_at": "2026-07-01T00:00:00Z",
    }


def test_drawer_transfer_does_not_claim_the_label_is_already_applied():
    """Approval only queues the label; execute --apply applies it."""
    text = " ".join(str(p) for p in app._drawer_transfer(_proposal("suggest-transfer")))
    assert "execute --apply" in text
    assert "Approving applied" not in text


def test_transfers_panel_prose_does_not_claim_the_label_is_already_applied():
    text = str(app.transfers_panel.content)
    assert "Approving applied" not in text
    assert "has been " in text and "applied" in text


def test_drawer_shows_the_duplicate_sibling_for_link_duplicate():
    """A link-duplicate posts a public comment naming the canonical, so the
    reviewer must see the sibling block (and its '(not found in mirror)' state)
    exactly as for close-duplicate."""
    parts = app._drawer_proposal(_proposal("link-duplicate"))
    text = " ".join(str(p) for p in parts)
    assert "Duplicate sibling" in text


def _result(pid="p1", status="applied", rid="r1", action="suggest-transfer"):
    return {
        "id": rid,
        "batch_id": "b1",
        "decision_id": f"d-{pid}",
        "proposal_id": pid,
        "repo": "rstudio/shiny",
        "issue": 7,
        "action": action,
        "status": status,
        "executed_at": "2026-07-02T00:00:00Z",
    }


def test_pending_transfers_hides_a_suggestion_whose_label_is_not_applied_yet(tmp_path):
    """Marking a row transferred writes a terminal decision that would cancel a
    still-unexecuted approval, so the row must not be offered until the
    'wrong location' label has actually been applied."""
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps(_approved()) + "\n", encoding="utf-8")
    r = tmp_path / "results"
    r.mkdir()

    # No results at all: approved but not executed.
    assert review_queue.pending_transfers(d, results_dir=r) == []

    # A dry-run result is not an application either.
    (r / "a.jsonl").write_text(
        json.dumps(_result(status="dry-run")) + "\n", encoding="utf-8"
    )
    assert review_queue.pending_transfers(d, results_dir=r) == []

    # Applied: now it is a real worklist item.
    with (r / "a.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_result(rid="r2")) + "\n")
    assert [
        x["proposal_id"] for x in review_queue.pending_transfers(d, results_dir=r)
    ] == ["p1"]


def test_pending_transfers_hides_a_suggestion_whose_label_was_undone(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps(_approved()) + "\n", encoding="utf-8")
    r = tmp_path / "results"
    r.mkdir()
    (r / "a.jsonl").write_text(
        json.dumps(_result(rid="r1"))
        + "\n"
        + json.dumps(
            {
                "id": "u1",
                "action": "undo",
                "status": "applied",
                "undoes_result_id": "r1",
                "repo": "rstudio/shiny",
                "issue": 7,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert review_queue.pending_transfers(d, results_dir=r) == []


def test_pending_transfers_without_results_dir_stays_approval_only(tmp_path):
    """Existing callers that pass no results log keep the old behaviour."""
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps(_approved()) + "\n", encoding="utf-8")

    assert [x["proposal_id"] for x in review_queue.pending_transfers(d)] == ["p1"]


def test_mark_transferred_removes_it_from_pending(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps(_approved()) + "\n", encoding="utf-8")
    assert [r["proposal_id"] for r in review_queue.pending_transfers(d)] == ["p1"]

    pid = app.app_mark_transferred(_approved(), decisions_dir=d)
    assert pid == "p1"
    assert review_queue.pending_transfers(d) == []


def test_mark_transferred_appends_rather_than_rewriting(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps(_approved()) + "\n", encoding="utf-8")
    app.app_mark_transferred(_approved(), decisions_dir=d)

    records = review_queue.iter_jsonl_records(d)
    verdicts = sorted(r["verdict"] for r in records)
    assert verdicts == ["approved", "transferred"]
