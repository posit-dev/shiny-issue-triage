"""proposals prune CLI."""

import json

from triage_verse import cli


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _seed(tmp_path):
    f = tmp_path / "proposals" / "2026" / "W27.jsonl"
    _write(
        f,
        [
            {"id": "bad-hyphen", "repo": "r/r", "issue": 1, "action": "add-label"},
            {"id": "keepme", "repo": "r/r", "issue": 2, "action": "add-label"},
        ],
    )
    return f


def test_prune_dry_run_reports_without_mutating(tmp_path, capsys):
    f = _seed(tmp_path)
    before = f.read_bytes()

    rc = cli.main(
        [
            "proposals",
            "prune",
            "bad-hyphen",
            "--proposals-dir",
            str(tmp_path / "proposals"),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "bad-hyphen" in out and "r/r#1" in out
    assert "analyze" in out  # points the operator at regeneration
    assert f.read_bytes() == before


def test_prune_apply_rewrites(tmp_path, capsys):
    f = _seed(tmp_path)

    rc = cli.main(
        [
            "proposals",
            "prune",
            "bad-hyphen",
            "--proposals-dir",
            str(tmp_path / "proposals"),
            "--apply",
        ]
    )

    assert rc == 0
    remaining = [json.loads(line) for line in f.read_text().splitlines()]
    assert [r["id"] for r in remaining] == ["keepme"]


def test_prune_valid_id_exits_nonzero(tmp_path, capsys):
    _seed(tmp_path)

    rc = cli.main(
        ["proposals", "prune", "keepme", "--proposals-dir", str(tmp_path / "proposals")]
    )

    assert rc == 1
    assert "valid module id" in capsys.readouterr().out
