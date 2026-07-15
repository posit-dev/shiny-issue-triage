"""prune_proposals: remove proposal records with invalid Shiny module ids."""

import json

import pytest

from triage_verse import proposals


def _rec(id, issue):
    return {"id": id, "repo": "r/r", "issue": issue, "action": "add-label"}


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_id_mode_removes_matching_record(tmp_path):
    f = tmp_path / "proposals" / "2026" / "W27.jsonl"
    _write(f, [_rec("bad-hyphen", 1), _rec("keepme", 2)])

    removed = proposals.prune_proposals(
        tmp_path / "proposals", "bad-hyphen", apply=True
    )

    assert [m["record"]["id"] for m in removed] == ["bad-hyphen"]
    assert removed[0]["record"]["issue"] == 1
    remaining = [json.loads(line) for line in f.read_text().splitlines()]
    assert [r["id"] for r in remaining] == ["keepme"]


def test_file_mode_removes_all_invalid_ids(tmp_path):
    f = tmp_path / "proposals" / "2026" / "W27.jsonl"
    _write(
        f,
        [
            _rec("ok", 1),
            _rec("bad-hyphen", 2),
            {"repo": "r/r", "issue": 3, "action": "add-label"},  # no id
            _rec("has space", 4),
        ],
    )

    removed = proposals.prune_proposals(tmp_path / "proposals", str(f), apply=True)

    assert sorted(m["record"]["issue"] for m in removed) == [2, 3, 4]
    remaining = [json.loads(line) for line in f.read_text().splitlines()]
    assert [r["id"] for r in remaining] == ["ok"]


def test_valid_id_target_refuses_and_changes_nothing(tmp_path):
    f = tmp_path / "proposals" / "2026" / "W27.jsonl"
    _write(f, [_rec("keepme", 1)])
    before = f.read_bytes()

    with pytest.raises(ValueError, match="valid module id"):
        proposals.prune_proposals(tmp_path / "proposals", "keepme", apply=True)

    assert f.read_bytes() == before


def test_dry_run_reports_but_does_not_mutate(tmp_path):
    f = tmp_path / "proposals" / "2026" / "W27.jsonl"
    _write(f, [_rec("bad-hyphen", 1), _rec("keepme", 2)])
    before = f.read_bytes()

    removed = proposals.prune_proposals(
        tmp_path / "proposals", "bad-hyphen", apply=False
    )

    assert [m["record"]["id"] for m in removed] == ["bad-hyphen"]
    assert removed[0]["file"] == str(f) and removed[0]["line"] == 1
    assert f.read_bytes() == before  # byte-for-byte unchanged


def test_rewrite_preserves_blank_and_malformed_lines(tmp_path):
    f = tmp_path / "proposals" / "2026" / "W27.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps(_rec("bad-hyphen", 1)) + "\n"
        "\n"
        "not json\n" + json.dumps(_rec("keepme", 2)) + "\n",
        encoding="utf-8",
    )

    proposals.prune_proposals(tmp_path / "proposals", str(f), apply=True)

    assert f.read_text(encoding="utf-8") == (
        "\nnot json\n" + json.dumps(_rec("keepme", 2)) + "\n"
    )


def test_missing_dir_returns_empty(tmp_path):
    assert proposals.prune_proposals(tmp_path / "nope", "bad-hyphen") == []
