"""Tier 1 session run: parsing, proposal emission, caps."""

import pytest

from triage_verse import config, db, review_queue, tier1


def _seed_fixed(con, repo, number):
    con.execute(
        "INSERT INTO issues (repo, number, title, body, state, updated_at,"
        " created_at, labels_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            repo,
            number,
            "Crash on load",
            "steps",
            "OPEN",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "[]",
        ),
    )
    con.execute(
        "INSERT INTO classifications (repo, number, clf_hash, type, priority,"
        " assessment, close_candidate_json, confidence, model, run_id, at)"
        " VALUES (?,?,'h','bug','Low','a',?,0.8,'m','run','2026-01-01T00:00:00Z')",
        (repo, number, '{"reason": "fixed", "rationale": "r", "confidence": 0.8}'),
    )


def _cfg(tmp_path):
    return config.load_models_config("config/models.yaml")


def test_parse_session_valid_and_invalid():
    ok = tier1.parse_session(
        '{"verdict": "fixed", "fixed_in": "v1.2",'
        ' "evidence": ["x"], "summary": "s", "confidence": 0.9}'
    )
    assert ok["verdict"] == "fixed"
    with pytest.raises(ValueError):
        tier1.parse_session("no json here")
    with pytest.raises(ValueError):
        tier1.parse_session('{"verdict": "banana"}')


def test_run_emits_close_proposal_on_fixed(tmp_path):
    con = db.connect(":memory:")
    _seed_fixed(con, "o/r", 1)

    def runner(repo_dir, prompt):
        return (
            '{"verdict": "fixed", "fixed_in": "v1.2", "evidence":'
            ' ["https://github.com/o/r/commit/abc"], "summary": "fixed in v1.2",'
            ' "confidence": 0.92}'
        ), 0.4

    def checkout(repo, cache_dir):
        return "/fake/repo"

    res = tier1.run(
        con,
        ["o/r"],
        cfg=_cfg(tmp_path),
        proposals_dir=tmp_path,
        run_gh=lambda *a, **k: "",
        runner=runner,
        checkout=checkout,
        log=lambda *a: None,
    )
    assert res["sessions"] == 1 and res["proposals"] == 1
    recs = review_queue.iter_jsonl_records(tmp_path)
    close = [r for r in recs if r["action"] == "close"]
    assert close[0]["origin"] == "tier1"
    assert close[0]["params"]["reason"] == "fixed"
    assert close[0]["repo"] == "o/r" and close[0]["issue"] == 1


def test_run_records_noop_on_not_fixed(tmp_path):
    con = db.connect(":memory:")
    _seed_fixed(con, "o/r", 1)

    def runner(repo_dir, prompt):
        return (
            '{"verdict": "not-fixed", "fixed_in": null, "evidence": [],'
            ' "summary": "still broken", "confidence": 0.7}',
            0.3,
        )

    res = tier1.run(
        con,
        ["o/r"],
        cfg=_cfg(tmp_path),
        proposals_dir=tmp_path,
        run_gh=lambda *a, **k: "",
        runner=runner,
        checkout=lambda r, c: "/fake",
        log=lambda *a: None,
    )
    assert res["proposals"] == 0
    recs = review_queue.iter_jsonl_records(tmp_path)
    assert recs[0]["action"] == "no-op" and recs[0]["origin"] == "tier1"


def test_run_stops_when_breaker_tripped(tmp_path, monkeypatch):
    con = db.connect(":memory:")
    _seed_fixed(con, "o/r", 1)
    monkeypatch.setattr(tier1.spend, "breaker_tripped", lambda con, cfg: True)
    res = tier1.run(
        con,
        ["o/r"],
        cfg=_cfg(tmp_path),
        proposals_dir=tmp_path,
        run_gh=lambda *a, **k: "",
        runner=lambda d, p: ("{}", 0.0),
        checkout=lambda r, c: "/fake",
        log=lambda *a: None,
    )
    assert res["sessions"] == 0 and res["halted_on_budget"] is True
