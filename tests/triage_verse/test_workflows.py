# tests/triage_verse/test_workflows.py
"""Dormancy + shape guards for the Plan 5 workflows."""

import pathlib

import yaml

WF = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load(name):
    text = (WF / name).read_text(encoding="utf-8")
    # PyYAML parses `on:` as boolean True key; keep both the parsed doc and raw text.
    return yaml.safe_load(text), text


def test_steady_state_is_dormant_dispatch_only():
    doc, text = _load("steady-state.yml")
    triggers = doc.get(True, doc.get("on"))
    assert "workflow_dispatch" in triggers
    # No active schedule: any cron line must be commented out.
    assert "schedule:" not in triggers if isinstance(triggers, dict) else True
    active_cron = [
        ln for ln in text.splitlines()
        if "cron:" in ln and not ln.strip().startswith("#")
    ]
    assert active_cron == []


def test_steady_state_has_no_issue_write_permission():
    doc, _ = _load("steady-state.yml")
    perms = doc.get("permissions", {})
    assert perms.get("issues", "none") != "write"
    assert perms.get("pull-requests", "none") != "write"
