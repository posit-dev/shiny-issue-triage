from triage_verse import config, db, dedup


def _pair():
    return (("r/a", 1, "ha"), ("r/b", 2, "hb"))


def test_build_requests_includes_both_issues(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    for repo, num in (("r/a", 1), ("r/b", 2)):
        con.execute(
            "INSERT INTO issues (repo, number, title, body, state, created_at,"
            " updated_at, is_pr) VALUES (?, ?, 'T', 'B', 'OPEN',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 0)",
            (repo, num),
        )
    con.commit()
    reqs = dedup.build_requests(
        con, _stage(), [{"type": "text", "text": "RUBRIC"}], [_pair()]
    )
    assert reqs[0].custom_id == "d0"
    assert reqs[0].params["model"] == "claude-sonnet-4-6"
    assert "r/a#1" in reqs[0].params["messages"][0]["content"]
    assert "r/b#2" in reqs[0].params["messages"][0]["content"]


def test_store_persists_canonical_pair(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    dedup.store(
        con,
        _pair(),
        {
            "verdict": "duplicate",
            "canonical": "r/a#1",
            "cross_repo_option": "close-and-link",
            "confidence": 0.8,
            "rationale": "same root cause",
        },
        "claude-sonnet-4-6",
        "run1",
    )
    row = db.get_dedup_verdict(con, "r/a", 1, "r/b", 2)
    assert row["verdict"] == "duplicate" and row["hash_a"] == "ha"


def test_parse_returns_none_on_errored_result():
    from triage_verse import llm

    assert dedup.parse(llm.BatchResult("x", "errored", error="invalid_request")) is None


def test_parse_returns_none_on_bad_json():
    from triage_verse import llm

    class _Block:
        type = "text"
        text = "not json{"

    class _Msg:
        content = [_Block()]

    assert dedup.parse(llm.BatchResult("x", "succeeded", message=_Msg())) is None


def _stage():
    return config.StageConfig("claude-sonnet-4-6", 1024)
