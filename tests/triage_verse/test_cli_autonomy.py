"""autonomy status CLI."""

import pathlib

import yaml

from triage_verse import cli, jsonl_log


def test_autonomy_status_write_promotes(tmp_path, monkeypatch, capsys):
    dec = tmp_path / "decisions"
    jsonl_log.append_weekly(
        [{"id": f"d{i}", "action": "add-label", "verdict": "approved"} for i in range(200)],
        dec,
    )
    out_yaml = tmp_path / "autonomy.yaml"
    rc = cli.main([
        "autonomy", "status", "--write",
        "--decisions-dir", str(dec), "--results-dir", str(tmp_path / "results"),
        "--out", str(out_yaml), "--models-config", "config/models.yaml",
    ])
    assert rc == 0
    doc = yaml.safe_load(out_yaml.read_text(encoding="utf-8"))
    assert "add-label" in doc["promoted"]
    assert "add-label" in capsys.readouterr().out
