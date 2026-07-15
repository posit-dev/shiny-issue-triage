import json

from triage_verse import db, jsonl_log, review_queue


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _mirror(tmp_path):
    return db.connect(tmp_path / "m.sqlite")


def _seed_issue(con, repo, number, state):
    db.upsert_issue(
        con,
        {
            "repo": repo,
            "number": number,
            "title": "T",
            "body": "B",
            "state": state,
            "state_reason": None,
            "author": "a",
            "labels_json": "[]",
            "assignees_json": "[]",
            "milestone": None,
            "comment_count": 0,
            "reaction_count": 0,
            "is_pr": 0,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "closed_at": None,
        },
    )
    con.commit()


def test_load_undecided_sorts_by_confidence_descending(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.9,
            },
            {
                "id": "b",
                "repo": "r/r",
                "issue": 2,
                "action": "add-label",
                "confidence": 0.3,
            },
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, _mirror(tmp_path))
    assert [r["id"] for r in rows] == ["a", "b"]


def test_load_undecided_excludes_terminal_verdicts(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.9,
            },
            {
                "id": "b",
                "repo": "r/r",
                "issue": 2,
                "action": "add-label",
                "confidence": 0.3,
            },
        ],
    )
    _write_jsonl(
        decisions_dir / "2026" / "W27.jsonl",
        [{"id": "d1", "proposal_id": "a", "verdict": "approved"}],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, _mirror(tmp_path))
    assert [r["id"] for r in rows] == ["b"]


def test_skip_defers_proposal_to_bottom_but_keeps_it(tmp_path):
    con = _mirror(tmp_path)
    _seed_issue(con, "r/r", 1, "OPEN")  # updated_at = 2026-01-01
    _seed_issue(con, "r/r", 2, "OPEN")
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.9,
            },
            {
                "id": "b",
                "repo": "r/r",
                "issue": 2,
                "action": "add-label",
                "confidence": 0.3,
            },
        ],
    )
    # Skip the higher-confidence "a" -> it stays but sinks below "b".
    _write_jsonl(
        decisions_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "d1",
                "proposal_id": "a",
                "verdict": "skipped",
                "decided_at": "2026-07-12T00:00:00Z",
            }
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, con)
    assert [r["id"] for r in rows] == ["b", "a"]
    assert rows[1]["deferred"] is True


def test_skip_cleared_when_issue_updates_after_skip(tmp_path):
    con = _mirror(tmp_path)
    _seed_issue(con, "r/r", 1, "OPEN")  # updated_at = 2026-01-01 (before the skip)
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.9,
            }
        ],
    )
    _write_jsonl(
        decisions_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "d1",
                "proposal_id": "a",
                "verdict": "skipped",
                "decided_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    # Issue unchanged since the skip -> still deferred.
    [row] = review_queue.load_undecided(proposals_dir, decisions_dir, con)
    assert row.get("deferred") is True
    # Issue updates after the skip -> deferral cleared, full priority again.
    con.execute(
        "UPDATE issues SET updated_at=? WHERE repo=? AND number=?",
        ("2026-07-20T00:00:00Z", "r/r", 1),
    )
    con.commit()
    [row] = review_queue.load_undecided(proposals_dir, decisions_dir, con)
    assert row.get("deferred") is not True


def test_load_undecided_skips_malformed_lines(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    path = proposals_dir / "2026" / "W27.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id": "a", "repo": "r/r", "issue": 1, "action": "add-label", "confidence": 0.5}\n'
        "not json\n",
        encoding="utf-8",
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, _mirror(tmp_path))
    assert [r["id"] for r in rows] == ["a"]


def test_load_undecided_missing_dirs_returns_empty(tmp_path):
    rows = review_queue.load_undecided(
        tmp_path / "nope-p", tmp_path / "nope-d", _mirror(tmp_path)
    )
    assert rows == []


def test_load_undecided_includes_close_actions(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.5,
            },
            {
                "id": "b",
                "repo": "r/r",
                "issue": 2,
                "action": "close",
                "confidence": 0.7,
            },
            {
                "id": "c",
                "repo": "r/r",
                "issue": 3,
                "action": "close-duplicate",
                "confidence": 0.6,
            },
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, _mirror(tmp_path))
    assert [r["id"] for r in rows] == ["b", "c", "a"]


def test_load_undecided_excludes_out_of_scope_actions(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.5,
            },
            {
                "id": "b",
                "repo": "r/r",
                "issue": 2,
                "action": "transfer",
                "confidence": 0.5,
            },
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, _mirror(tmp_path))
    assert [r["id"] for r in rows] == ["a"]


def test_close_proposal_leaves_queue_when_issue_closes(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "close",
                "confidence": 0.9,
            }
        ],
    )
    con = _mirror(tmp_path)
    _seed_issue(con, "r/r", 1, "CLOSED")
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, con)
    assert rows == []


def test_high_stakes_actions_are_supported():
    assert review_queue.HIGH_STAKES_ACTIONS <= review_queue.SUPPORTED_ACTIONS
    assert review_queue.HIGH_STAKES_ACTIONS == frozenset({"close", "close-duplicate"})


def test_load_undecided_excludes_closed_issues(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.5,
            },
            {
                "id": "b",
                "repo": "r/r",
                "issue": 2,
                "action": "add-label",
                "confidence": 0.6,
            },
        ],
    )
    con = _mirror(tmp_path)
    _seed_issue(con, "r/r", 1, "OPEN")
    _seed_issue(con, "r/r", 2, "CLOSED")
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, con)
    assert [r["id"] for r in rows] == ["a"]


def test_load_undecided_keeps_proposals_missing_from_mirror(tmp_path):
    proposals_dir = tmp_path / "proposals"
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        proposals_dir / "2026" / "W27.jsonl",
        [
            {
                "id": "a",
                "repo": "r/r",
                "issue": 1,
                "action": "add-label",
                "confidence": 0.5,
            }
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir, _mirror(tmp_path))
    assert [r["id"] for r in rows] == ["a"]


def test_issue_snippet_truncates_long_body():
    snippet = review_queue.issue_snippet("Title", "x" * 300, max_chars=280)
    assert snippet.startswith("Title\n\n")
    assert snippet.endswith("…")
    assert len(snippet) < len("Title\n\n") + 300


def test_issue_snippet_handles_missing_body():
    assert review_queue.issue_snippet("Title", None) == "Title"


def test_duplicate_sibling_returns_other_issue():
    proposal = {
        "repo": "r/a",
        "issue": 1,
        "evidence": [
            "https://github.com/r/a/issues/1",
            "https://github.com/r/b/issues/2",
        ],
    }
    assert review_queue.duplicate_sibling(proposal) == ("r/b", 2)


def test_duplicate_sibling_handles_sibling_listed_first():
    proposal = {
        "repo": "r/a",
        "issue": 1,
        "evidence": [
            "https://github.com/r/b/issues/2",
            "https://github.com/r/a/issues/1",
        ],
    }
    assert review_queue.duplicate_sibling(proposal) == ("r/b", 2)


def test_duplicate_sibling_none_when_self_only():
    proposal = {
        "repo": "r/a",
        "issue": 1,
        "evidence": ["https://github.com/r/a/issues/1"],
    }
    assert review_queue.duplicate_sibling(proposal) is None


def test_duplicate_sibling_none_when_evidence_missing():
    assert review_queue.duplicate_sibling({"repo": "r/a", "issue": 1}) is None


def test_duplicate_sibling_skips_malformed_urls():
    proposal = {
        "repo": "r/a",
        "issue": 1,
        "evidence": [
            "not a url",
            "https://github.com/r/b/pull/9",
            "https://github.com/r/b/issues/not-a-number",
            "https://github.com/r/b/issues/2",
        ],
    }
    assert review_queue.duplicate_sibling(proposal) == ("r/b", 2)


def test_key_actions_cover_documented_bindings():
    assert review_queue.KEY_ACTIONS == {
        "j": "next",
        "k": "prev",
        "ArrowDown": "next",
        "ArrowUp": "prev",
        "a": "approve",
        "r": "reject",
        "s": "skip",
        "e": "edit",
        "Enter": "toggle",
        "Escape": "close",
    }


def test_stale_result_resurfaces_proposal(tmp_path):
    con = _mirror(tmp_path)
    _seed_issue(con, "o/r", 1, "OPEN")
    proposal = {
        "id": "p1",
        "repo": "o/r",
        "issue": 1,
        "action": "add-label",
        "params": {"label": "regression"},
        "confidence": 0.9,
    }
    jsonl_log.append_weekly([proposal], tmp_path / "proposals")
    jsonl_log.append_weekly(
        [
            {
                "id": "d1",
                "proposal_id": "p1",
                "verdict": "approved",
                "decided_at": "2026-07-12T00:00:00Z",
            }
        ],
        tmp_path / "decisions",
    )
    jsonl_log.append_weekly(
        [
            {
                "id": "r1",
                "proposal_id": "p1",
                "status": "stale-needs-rereview",
                "executed_at": "2026-07-13T00:00:00Z",
            }
        ],
        tmp_path / "results",
    )
    # Without results_dir: hidden (decided). With: resurfaces, flagged stale.
    assert (
        review_queue.load_undecided(tmp_path / "proposals", tmp_path / "decisions", con)
        == []
    )
    [row] = review_queue.load_undecided(
        tmp_path / "proposals",
        tmp_path / "decisions",
        con,
        results_dir=tmp_path / "results",
    )
    assert row["id"] == "p1" and row["stale"] is True


def test_fresh_decision_after_stale_result_hides_again(tmp_path):
    con = _mirror(tmp_path)
    _seed_issue(con, "o/r", 1, "OPEN")
    proposal = {
        "id": "p1",
        "repo": "o/r",
        "issue": 1,
        "action": "add-label",
        "params": {"label": "regression"},
        "confidence": 0.9,
    }
    jsonl_log.append_weekly([proposal], tmp_path / "proposals")
    jsonl_log.append_weekly(
        [
            {
                "id": "r1",
                "proposal_id": "p1",
                "status": "stale-needs-rereview",
                "executed_at": "2026-07-13T00:00:00Z",
            }
        ],
        tmp_path / "results",
    )
    jsonl_log.append_weekly(
        [
            {
                "id": "d2",
                "proposal_id": "p1",
                "verdict": "rejected",
                "decided_at": "2026-07-14T00:00:00Z",
            }
        ],
        tmp_path / "decisions",
    )
    assert (
        review_queue.load_undecided(
            tmp_path / "proposals",
            tmp_path / "decisions",
            con,
            results_dir=tmp_path / "results",
        )
        == []
    )


def test_clamp_index():
    assert review_queue.clamp_index(None, 0) is None
    assert review_queue.clamp_index(3, 0) is None
    assert review_queue.clamp_index(None, 5) == 0
    assert review_queue.clamp_index(-1, 5) == 0
    assert review_queue.clamp_index(2, 5) == 2
    assert review_queue.clamp_index(7, 5) == 4  # queue shrank under selection
