import json

from triage_verse import review_queue


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_load_undecided_sorts_by_confidence_ascending(tmp_path):
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
    rows = review_queue.load_undecided(proposals_dir, decisions_dir)
    assert [r["id"] for r in rows] == ["b", "a"]


def test_load_undecided_excludes_any_verdict(tmp_path):
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
        [{"id": "d1", "proposal_id": "a", "verdict": "skipped"}],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir)
    assert [r["id"] for r in rows] == ["b"]


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
    rows = review_queue.load_undecided(proposals_dir, decisions_dir)
    assert [r["id"] for r in rows] == ["a"]


def test_load_undecided_missing_dirs_returns_empty(tmp_path):
    rows = review_queue.load_undecided(tmp_path / "nope-p", tmp_path / "nope-d")
    assert rows == []


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
                "action": "close",
                "confidence": 0.5,
            },
            {
                "id": "c",
                "repo": "r/r",
                "issue": 3,
                "action": "close-duplicate",
                "confidence": 0.5,
            },
        ],
    )
    rows = review_queue.load_undecided(proposals_dir, decisions_dir)
    assert [r["id"] for r in rows] == ["a"]


def test_issue_snippet_truncates_long_body():
    snippet = review_queue.issue_snippet("Title", "x" * 300, max_chars=280)
    assert snippet.startswith("Title\n\n")
    assert snippet.endswith("…")
    assert len(snippet) < len("Title\n\n") + 300


def test_issue_snippet_handles_missing_body():
    assert review_queue.issue_snippet("Title", None) == "Title"
