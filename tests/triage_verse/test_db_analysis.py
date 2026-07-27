# tests/triage_verse/test_db_analysis.py
import pytest
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
            "model": "claude-sonnet-5",
            "run_id": "run1",
            "at": "2026-06-29T01:00:00Z",
        },
    )
    row = db.get_classification(con, "r/r", 1)
    assert row["type"] == "feat" and row["clf_hash"] == "h2"
    assert db.get_classification(con, "r/r", 2) is None


def test_classification_append_across_runs(tmp_path):
    con = _con(tmp_path)
    base = {
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
    }
    db.upsert_classification(con, base)
    db.upsert_classification(
        con,
        {
            **base,
            "clf_hash": "h2",
            "type": "feat",
            "confidence": 0.5,
            "model": "claude-sonnet-5",
            "run_id": "run2",
            "at": "2026-06-29T02:00:00Z",
        },
    )
    # Both runs retained as history.
    rows = con.execute(
        "SELECT run_id, type FROM classifications "
        "WHERE repo='r/r' AND number=1 ORDER BY at"
    ).fetchall()
    assert [(r["run_id"], r["type"]) for r in rows] == [
        ("run1", "fix"),
        ("run2", "feat"),
    ]
    # Latest view / get_classification return the newest run.
    row = db.get_classification(con, "r/r", 1)
    assert row["run_id"] == "run2" and row["type"] == "feat"


def test_classification_recheck_updates_same_run(tmp_path):
    con = _con(tmp_path)
    base = {
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
    }
    db.upsert_classification(con, base)  # classify
    db.upsert_classification(
        con, {**base, "type": "feat", "confidence": 0.4, "at": "2026-06-29T00:05:00Z"}
    )  # recheck, same run_id
    row = con.execute(
        "SELECT COUNT(*) c FROM classifications WHERE repo='r/r' AND number=1"
    ).fetchone()
    assert row["c"] == 1  # one row per run
    assert db.get_classification(con, "r/r", 1)["type"] == "feat"


def test_dedup_append_across_runs(tmp_path):
    con = _con(tmp_path)
    base = {
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
        "model": "claude-sonnet-5",
        "run_id": "run1",
        "at": "2026-06-29T00:00:00Z",
    }
    db.upsert_dedup_verdict(con, base)
    db.upsert_dedup_verdict(
        con,
        {
            **base,
            "verdict": "not-duplicate",
            "run_id": "run2",
            "at": "2026-06-29T02:00:00Z",
        },
    )
    assert con.execute("SELECT COUNT(*) c FROM dedup_verdicts").fetchone()["c"] == 2
    assert db.get_dedup_verdict(con, "r/a", 1, "r/b", 2)["verdict"] == "not-duplicate"


def test_full_history_query(tmp_path):
    con = _con(tmp_path)
    base = {
        "repo": "r/r",
        "number": 1,
        "clf_hash": "h",
        "type": "fix",
        "priority": "High",
        "assessment": "actionable",
        "labels_json": "[]",
        "close_candidate_json": None,
        "confidence": 0.9,
        "model": "m1",
        "run_id": "run1",
        "at": "2026-06-01T00:00:00Z",
    }
    db.upsert_classification(con, base)
    db.upsert_classification(
        con,
        {
            **base,
            "run_id": "run2",
            "model": "m2",
            "priority": "Low",
            "at": "2026-06-08T00:00:00Z",
        },
    )
    history = con.execute(
        "SELECT run_id, model, priority, at FROM classifications "
        "WHERE repo='r/r' AND number=1 ORDER BY at"
    ).fetchall()
    assert [(r["run_id"], r["model"], r["priority"]) for r in history] == [
        ("run1", "m1", "High"),
        ("run2", "m2", "Low"),
    ]


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
            "model": "claude-sonnet-5",
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
    db.insert_spend(con, "run1", "dedup", "claude-sonnet-5", 2000, 0, 300, 0.0052)
    assert round(db.today_spend_usd(con), 4) == 0.0067


def test_set_batch_rejects_unknown_and_empty(tmp_path):
    con = _con(tmp_path)
    db.insert_batch(con, "b1", "run1", "classify", "prov1", 1)
    with pytest.raises(ValueError):
        db.set_batch(con, "b1")
    with pytest.raises(ValueError):
        db.set_batch(con, "b1", bogus_column="x")
    db.set_batch(con, "b1", status="collected")  # allowed field still works
    assert db.open_batches(con) == []
