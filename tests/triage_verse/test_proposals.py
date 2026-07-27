# tests/triage_verse/test_proposals.py
import json

import pytest

from triage_verse import db, proposals


def _seed(con):
    con.execute(
        "INSERT INTO issues (repo, number, title, state, created_at,"
        " updated_at, is_pr) VALUES ('r/r', 1, 'T', 'OPEN',"
        " '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z', 0)"
    )
    db.upsert_classification(
        con,
        {
            "repo": "r/r",
            "number": 1,
            "clf_hash": "h",
            "type": "fix",
            "priority": "High",
            "assessment": "actionable",
            "labels_json": json.dumps(["needs reprex"]),
            "close_candidate_json": json.dumps(
                {"reason": "fixed", "rationale": "v1.2", "confidence": 0.95}
            ),
            "confidence": 0.95,
            "model": "claude-sonnet-5",
            "run_id": "run1",
            "at": "2026-06-29T00:00:00Z",
        },
    )
    con.commit()


def test_build_emits_label_priority_and_close(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)
    recs = proposals.build(con, "run1")
    actions = {r["action"] for r in recs}
    assert {"add-label", "set-priority", "close"} <= actions
    close = next(r for r in recs if r["action"] == "close")
    assert close["issue_updated_at"] == "2026-06-01T00:00:00Z"
    assert close["params"]["reason"] == "fixed"


def test_build_emits_close_duplicate_with_freshness(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    con.execute(
        "INSERT INTO issues (repo, number, title, state, created_at,"
        " updated_at, is_pr) VALUES ('r/a', 1, 'T', 'OPEN',"
        " '2026-01-01T00:00:00Z', '2026-06-02T00:00:00Z', 0)"
    )
    db.upsert_dedup_verdict(
        con,
        {
            "repo_a": "r/a",
            "number_a": 1,
            "repo_b": "r/b",
            "number_b": 2,
            "hash_a": "ha",
            "hash_b": "hb",
            "verdict": "duplicate",
            "canonical_json": '"r/a#1"',
            "cross_repo_option": "close-and-link",
            "confidence": 0.9,
            "rationale": "same",
            "model": "claude-sonnet-5",
            "run_id": "run1",
            "at": "2026-06-29T00:00:00Z",
        },
    )
    con.commit()
    recs = proposals.build(con, "run1")
    dup = next(r for r in recs if r["action"] == "close-duplicate")
    assert dup["issue_updated_at"] == "2026-06-02T00:00:00Z"
    assert dup["params"]["canonical"] == "r/a#1"


def test_write_appends_weekly_partition(tmp_path):
    recs = [
        {
            "id": "x",
            "repo": "r/r",
            "issue": 1,
            "action": "add-label",
            "params": {"label": "needs reprex"},
            "rationale": "",
            "confidence": 0.9,
            "evidence": [],
            "issue_updated_at": "2026-06-01T00:00:00Z",
            "run_id": "run1",
            "model": "claude-haiku-4-5",
        }
    ]
    path = proposals.write(recs, tmp_path / "proposals", today="2026-06-29")
    assert path.exists()
    assert "2026/W27.jsonl" in str(path).replace("\\", "/")
    line = json.loads(path.read_text().splitlines()[0])
    assert line["action"] == "add-label"
    # appends, not overwrites
    proposals.write(recs, tmp_path / "proposals", today="2026-06-29")
    assert len(path.read_text().splitlines()) == 2


def _seed_dup(con, option, repo_b="r/b"):
    """One duplicate verdict between r/a#1 and repo_b#2 with `option`."""
    for repo, num in (("r/a", 1), (repo_b, 2)):
        con.execute(
            "INSERT OR IGNORE INTO issues (repo, number, title, state, created_at,"
            " updated_at, is_pr) VALUES (?, ?, 'T', 'OPEN',"
            " '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z', 0)",
            (repo, num),
        )
    db.upsert_dedup_verdict(
        con,
        {
            "repo_a": "r/a",
            "number_a": 1,
            "repo_b": repo_b,
            "number_b": 2,
            "hash_a": "ha",
            "hash_b": "hb",
            "verdict": "duplicate",
            "canonical_json": json.dumps(f"{repo_b}#2"),
            "cross_repo_option": option,
            "confidence": 0.9,
            "rationale": "same bug",
            "model": "claude-sonnet-5",
            "run_id": "run1",
            "at": "2026-06-29T00:00:00Z",
        },
    )
    con.commit()


@pytest.mark.parametrize(
    "option,expected",
    [
        ("close-and-link", "close-duplicate"),
        (None, "close-duplicate"),
        ("nonsense", "close-duplicate"),
        ("keep-both-link", "link-duplicate"),
        ("transfer", "suggest-transfer"),
    ],
)
def test_build_maps_cross_repo_option_to_action(tmp_path, option, expected):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_dup(con, option)
    recs = [r for r in proposals.build(con, "run1") if r["repo"] == "r/a"]
    assert [r["action"] for r in recs] == [expected]
    assert recs[0]["params"]["canonical"] == "r/b#2"
    assert recs[0]["params"]["cross_repo_option"] == option


@pytest.mark.parametrize("option", ["transfer", "keep-both-link"])
def test_same_repo_pair_ignores_cross_repo_option(tmp_path, option):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_dup(con, option, repo_b="r/a")
    recs = [r for r in proposals.build(con, "run1") if r["repo"] == "r/a"]
    assert [r["action"] for r in recs] == ["close-duplicate"]
