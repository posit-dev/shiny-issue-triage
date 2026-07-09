import json

from triage_verse import dashboard, db


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _mirror(tmp_path):
    return db.connect(tmp_path / "m.sqlite")


def _seed_issue(con, repo, number, state, closed_at=None):
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
            "closed_at": closed_at,
        },
    )
    con.commit()


def _seed_classification(con, repo, number):
    db.upsert_classification(
        con,
        {
            "repo": repo,
            "number": number,
            "clf_hash": "h",
            "type": "bug",
            "priority": "P2",
            "assessment": "ok",
            "labels_json": "[]",
            "close_candidate_json": None,
            "confidence": 0.9,
            "model": "m",
            "run_id": "r1",
            "at": "2026-01-05T00:00:00Z",
        },
    )
    con.commit()


def test_triage_coverage_counts_classified_open_issues(tmp_path):
    con = _mirror(tmp_path)
    _seed_issue(con, "org/a", 1, "OPEN")
    _seed_issue(con, "org/a", 2, "OPEN")
    _seed_issue(con, "org/a", 3, "CLOSED", closed_at="2026-02-01T00:00:00Z")
    _seed_issue(con, "org/b", 1, "OPEN")
    _seed_classification(con, "org/a", 1)
    _seed_classification(con, "org/a", 3)  # closed: not counted

    rows = dashboard.triage_coverage(con)

    assert rows == [
        {"repo": "org/a", "open": 2, "classified": 1, "pct": 50.0},
        {"repo": "org/b", "open": 1, "classified": 0, "pct": 0.0},
        {"repo": "TOTAL", "open": 3, "classified": 1, "pct": 100 / 3},
    ]


def test_triage_coverage_empty_mirror(tmp_path):
    con = _mirror(tmp_path)
    assert dashboard.triage_coverage(con) == []


def test_weekly_throughput_buckets_by_iso_week(tmp_path):
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        decisions_dir / "2026" / "W01.jsonl",
        [
            {"id": "d1", "verdict": "approved", "decided_at": "2026-01-01T10:00:00Z"},
            {"id": "d2", "verdict": "rejected", "decided_at": "2026-01-02T10:00:00Z"},
            {"id": "d3", "verdict": "approved", "decided_at": "2026-01-08T10:00:00Z"},
            {"id": "bad", "verdict": "approved"},  # missing decided_at: skipped
        ],
    )
    assert dashboard.weekly_throughput(decisions_dir) == [
        {"week": "2026-W01", "decided": 2},
        {"week": "2026-W02", "decided": 1},
    ]


def test_weekly_throughput_missing_dir(tmp_path):
    assert dashboard.weekly_throughput(tmp_path / "nope") == []


def test_category_precision_per_action(tmp_path):
    decisions_dir = tmp_path / "decisions"
    _write_jsonl(
        decisions_dir / "2026" / "W01.jsonl",
        [
            {"action": "add-label", "verdict": "approved"},
            {"action": "add-label", "verdict": "approved"},
            {"action": "add-label", "verdict": "rejected"},
            {"action": "add-label", "verdict": "skipped"},
            {"action": "set-priority", "verdict": "skipped"},
            {"verdict": "approved"},  # missing action: skipped
        ],
    )
    assert dashboard.category_precision(decisions_dir) == [
        {
            "action": "add-label",
            "approved": 2,
            "rejected": 1,
            "skipped": 1,
            "precision": 2 / 3,
        },
        {
            "action": "set-priority",
            "approved": 0,
            "rejected": 0,
            "skipped": 1,
            "precision": None,
        },
    ]


def test_stage_spend_aggregates_and_orders_by_usd(tmp_path):
    con = _mirror(tmp_path)
    db.insert_spend(con, "r1", "classify", "m", 100, 10, 20, 0.5)
    db.insert_spend(con, "r1", "classify", "m", 100, 10, 20, 0.25)
    db.insert_spend(con, "r2", "dedup", "m", 50, 5, 10, 2.0)
    con.commit()

    assert dashboard.stage_spend(con) == [
        {
            "stage": "dedup",
            "calls": 1,
            "input_tokens": 50,
            "cached_tokens": 5,
            "output_tokens": 10,
            "usd": 2.0,
        },
        {
            "stage": "classify",
            "calls": 2,
            "input_tokens": 200,
            "cached_tokens": 20,
            "output_tokens": 40,
            "usd": 0.75,
        },
    ]


def test_stage_spend_empty(tmp_path):
    assert dashboard.stage_spend(_mirror(tmp_path)) == []


def test_svg_line_chart_renders_each_series():
    svg = dashboard.svg_line_chart(
        {
            "opened": [("2026-W01", 3), ("2026-W02", 5)],
            "closed": [("2026-W01", 1), ("2026-W02", 6)],
        }
    )
    assert svg.startswith("<svg")
    assert svg.count("<polyline") == 2
    assert "opened" in svg and "closed" in svg


def test_svg_line_chart_single_series_uses_first_color():
    svg = dashboard.svg_line_chart({"open": [("2026-W01", 3), ("2026-W02", 4)]})
    assert svg.count("<polyline") == 1
    assert dashboard.SERIES_COLORS[0] in svg


def test_svg_line_chart_empty_returns_placeholder():
    svg = dashboard.svg_line_chart({})
    assert "no data" in svg
    assert "<polyline" not in svg


def test_svg_line_chart_flat_series_does_not_crash():
    svg = dashboard.svg_line_chart({"open": [("2026-W01", 2), ("2026-W02", 2)]})
    assert "<polyline" in svg
