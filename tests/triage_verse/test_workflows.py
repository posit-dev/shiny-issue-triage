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


def test_tier2_fix_is_dispatch_only_with_issue_input():
    doc, text = _load("tier2-fix.yml")
    triggers = doc.get(True, doc.get("on"))
    assert "workflow_dispatch" in triggers
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "issue" in inputs and inputs["issue"]["required"] is True
    assert "model" in inputs
    active_cron = [ln for ln in text.splitlines()
                   if "cron:" in ln and not ln.strip().startswith("#")]
    assert active_cron == []


def test_tier2_fix_guards_label_and_weekly_cap():
    _, text = _load("tier2-fix.yml")
    assert "ai-triage:fix-requested" in text  # label guard present
    assert "gh run list" in text  # weekly-cap guard present
    assert "--draft" in text  # PR opened as draft
