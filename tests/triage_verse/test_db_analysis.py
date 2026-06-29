# tests/triage_verse/test_db_analysis.py
from triage_verse import db


def _con(tmp_path):
    return db.connect(tmp_path / "m.sqlite")


def test_classification_upsert_roundtrip(tmp_path):
    con = _con(tmp_path)
    db.upsert_classification(
        con,
        {
            "repo": "r/r",
            "number": 1,
            "clf_hash": "h1",
            "type": "fix",
            "priority": "High",
            "assessment": "actionable",
            "labels_json": "[]",
            "close_candidate_json": None,
            "confidence": 0.9,
            "model": "claude-haiku-4-5",
            "run_id": "run1",
            "at": "2026-06-29T00:00:00Z",
        },
    )
    db.upsert_classification(
        con,
        {
            "repo": "r/r",
            "number": 1,
            "clf_hash": "h2",
            "type": "feat",
            "priority": "Low",
            "assessment": "actionable",
            "labels_json": "[]",
            "close_candidate_json": None,
            "confidence": 0.5,
            "model": "claude-sonnet-4-6",
            "run_id": "run1",
            "at": "2026-06-29T01:00:00Z",
        },
    )
    row = db.get_classification(con, "r/r", 1)
    assert row["type"] == "feat" and row["clf_hash"] == "h2"
    assert db.get_classification(con, "r/r", 2) is None


def test_dedup_verdict_roundtrip(tmp_path):
    con = _con(tmp_path)
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
            "confidence": 0.8,
            "rationale": "same",
            "model": "claude-sonnet-4-6",
            "run_id": "run1",
            "at": "2026-06-29T00:00:00Z",
        },
    )
    row = db.get_dedup_verdict(con, "r/a", 1, "r/b", 2)
    assert row["verdict"] == "duplicate"


def test_batch_lifecycle(tmp_path):
    con = _con(tmp_path)
    db.insert_batch(con, "b1", "run1", "classify", "prov1", 3)
    db.insert_batch_items(con, "b1", {"c0": '["r/r", 1]', "c1": '["r/r", 2]'})
    assert [r["batch_id"] for r in db.open_batches(con)] == ["b1"]
    assert db.get_batch_items(con, "b1")["c1"] == '["r/r", 2]'
    db.set_batch(con, "b1", status="collected", ended_at="2026-06-29T02:00:00Z")
    assert db.open_batches(con) == []
    assert [r["batch_id"] for r in db.run_batches(con, "run1")] == ["b1"]


def test_spend_and_today_total(tmp_path):
    con = _con(tmp_path)
    db.insert_spend(con, "run1", "classify", "claude-haiku-4-5", 1000, 0, 200, 0.0015)
    db.insert_spend(con, "run1", "dedup", "claude-sonnet-4-6", 2000, 0, 300, 0.0052)
    assert round(db.today_spend_usd(con), 4) == 0.0067
