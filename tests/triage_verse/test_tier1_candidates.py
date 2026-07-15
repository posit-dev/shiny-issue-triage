"""Tier 1 candidate selection from the mirror."""

from triage_verse import db, jsonl_log, tier1


def _open_issue(con, repo, number, updated="2026-01-01T00:00:00Z"):
    con.execute(
        "INSERT INTO issues (repo, number, title, state, updated_at, created_at,"
        " labels_json) VALUES (?,?,?,?,?,?,?)",
        (repo, number, "t", "OPEN", updated, updated, "[]"),
    )


def test_selects_fixed_close_candidates_and_merged_pr_refs(tmp_path):
    con = db.connect(":memory:")
    _open_issue(con, "o/r", 1)
    _open_issue(con, "o/r", 2)
    _open_issue(con, "o/r", 3)
    con.execute(
        "INSERT INTO classifications (repo, number, clf_hash, type, priority,"
        " assessment, close_candidate_json, confidence, model, run_id, at)"
        " VALUES ('o/r',1,'h','bug','Low','a',?,0.8,'m','run','2026-01-01T00:00:00Z')",
        ('{"reason": "fixed", "rationale": "r", "confidence": 0.8}',),
    )
    # issue 2 referenced by a merged PR
    con.execute(
        "INSERT INTO prs (repo, number, merged, closing_issue_refs_json)"
        " VALUES ('o/r',99,1,'[2]')"
    )
    cands = tier1.select_candidates(con, ["o/r"], proposals_dir=tmp_path, limit=25)
    nums = {c["issue"] for c in cands}
    assert nums == {1, 2}  # 3 has no signal


def test_excludes_issues_with_existing_tier1_proposal(tmp_path):
    con = db.connect(":memory:")
    _open_issue(con, "o/r", 1)
    con.execute(
        "INSERT INTO classifications (repo, number, clf_hash, type, priority,"
        " assessment, close_candidate_json, confidence, model, run_id, at)"
        " VALUES ('o/r',1,'h','bug','Low','a',?,0.8,'m','run','2026-01-01T00:00:00Z')",
        ('{"reason": "fixed", "rationale": "r", "confidence": 0.8}',),
    )
    jsonl_log.append_weekly(
        [{"id": "x", "repo": "o/r", "issue": 1, "origin": "tier1", "action": "no-op"}],
        tmp_path,
    )
    cands = tier1.select_candidates(con, ["o/r"], proposals_dir=tmp_path, limit=25)
    assert cands == []


def test_limit_caps_and_orders_oldest_first(tmp_path):
    con = db.connect(":memory:")
    _open_issue(con, "o/r", 1, "2026-03-01T00:00:00Z")
    _open_issue(con, "o/r", 2, "2026-01-01T00:00:00Z")
    for n in (1, 2):
        con.execute(
            "INSERT INTO classifications (repo, number, clf_hash, type, priority,"
            " assessment, close_candidate_json, confidence, model, run_id, at)"
            " VALUES ('o/r',?,'h','bug','Low','a',?,0.8,'m','run','2026-01-01T00:00:00Z')",
            (n, '{"reason": "fixed", "rationale": "r", "confidence": 0.8}'),
        )
    cands = tier1.select_candidates(con, ["o/r"], proposals_dir=tmp_path, limit=1)
    assert cands == [{"repo": "o/r", "issue": 2}]  # oldest updated_at first
