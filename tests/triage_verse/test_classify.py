from triage_verse import classify, config, db


def test_clf_hash_changes_with_comments():
    a = classify.clf_hash("t", "b", ["c1"])
    b = classify.clf_hash("t", "b", ["c1", "c2"])
    assert a != b


def test_needs_recheck_on_low_confidence_or_close_candidate():
    assert classify.needs_recheck({"confidence": 0.5, "close_candidate": None}, 0.7)
    assert classify.needs_recheck(
        {"confidence": 0.99, "close_candidate": {"reason": "fixed"}}, 0.7
    )
    assert not classify.needs_recheck({"confidence": 0.9, "close_candidate": None}, 0.7)


def test_build_requests_uses_prefix_and_schema(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    rows = [{"repo": "r/r", "number": 1, "title": "boom", "body": "trace"}]
    reqs = classify.build_requests(
        con,
        _cfg(),
        _cfg().classify,
        [{"type": "text", "text": "RUBRIC", "cache_control": {"type": "ephemeral"}}],
        rows,
        prefix="c",
    )
    assert reqs[0].custom_id == "c0"
    assert reqs[0].params["model"] == "claude-haiku-4-5"
    assert reqs[0].params["output_config"]["format"]["type"] == "json_schema"
    assert "<ISSUE_TITLE>" in reqs[0].params["messages"][0]["content"]


def test_store_drops_unknown_labels(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    data = {
        "type": "fix",
        "priority": "High",
        "assessment": "actionable",
        "labels": ["needs reprex", "bogus"],
        "close_candidate": None,
        "confidence": 0.9,
    }
    classify.store(
        con, "r/r", 1, "h", data, "claude-haiku-4-5", "run1", allowed={"needs reprex"}
    )
    import json

    row = db.get_classification(con, "r/r", 1)
    assert json.loads(row["labels_json"]) == ["needs reprex"]


def test_clf_hash_no_field_collision():
    # different title/body split must not collide
    assert classify.clf_hash("ab", "c", []) != classify.clf_hash("a", "bc", [])
    # different comment grouping must not collide
    assert classify.clf_hash("t", "b", ["a", "b"]) != classify.clf_hash(
        "t", "b", ["ab"]
    )


def test_parse_returns_none_on_errored_result():
    from triage_verse import llm

    assert (
        classify.parse(llm.BatchResult("x", "errored", error="invalid_request")) is None
    )


def test_parse_returns_none_on_bad_json():
    from triage_verse import llm

    class _Block:
        type = "text"
        text = "not json{"

    class _Msg:
        content = [_Block()]

    assert classify.parse(llm.BatchResult("x", "succeeded", message=_Msg())) is None


def _cfg():
    return config.ModelsConfig(
        "m",
        8,
        10,
        0.8,
        config.StageConfig("claude-haiku-4-5", 512),
        config.StageConfig("claude-sonnet-4-6", 1024, 0.7),
        config.StageConfig("claude-sonnet-4-6", 1024),
        500,
        30,
        True,
        50,
        {},
    )
