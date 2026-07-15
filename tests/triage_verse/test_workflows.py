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
        ln
        for ln in text.splitlines()
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
    active_cron = [
        ln
        for ln in text.splitlines()
        if "cron:" in ln and not ln.strip().startswith("#")
    ]
    assert active_cron == []


def test_tier2_fix_guards_label_and_weekly_cap():
    _, text = _load("tier2-fix.yml")
    assert "ai-triage:fix-requested" in text  # label guard present
    assert "gh run list" in text  # weekly-cap guard present
    assert "--draft" in text  # PR opened as draft


def test_reprex_is_dispatch_only_with_issue_input():
    doc, text = _load("reprex.yml")
    triggers = doc.get(True, doc.get("on"))
    assert "workflow_dispatch" in triggers
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "issue" in inputs and inputs["issue"]["required"] is True
    active_cron = [
        ln
        for ln in text.splitlines()
        if "cron:" in ln and not ln.strip().startswith("#")
    ]
    assert active_cron == []


def test_reprex_guards_label_and_never_auto_closes():
    doc, text = _load("reprex.yml")
    assert "ai-triage:needs-reprex" in text  # label guard present
    assert "gh run list" in text  # weekly-cap guard present
    # Non-reproducible path labels for human review instead of closing.
    assert "ai-triage:no-reprex" in text
    assert "ai-triage:needs-review" in text
    # High-stakes closes are never automated: no `gh issue close` in the workflow.
    assert "gh issue close" not in text
    # No issue-write permission is granted at the workflow level.
    assert doc.get("permissions", {}).get("issues", "none") != "write"


def test_reprex_input_not_interpolated_into_run_blocks():
    # Script-injection guard: the untrusted issue input reaches shell only as an
    # `env:` assignment, never interpolated directly into a shell command.
    _, text = _load("reprex.yml")
    inputs_lines = [ln for ln in text.splitlines() if "github.event.inputs" in ln]
    assert inputs_lines  # the input is referenced somewhere
    for ln in inputs_lines:
        assert ln.strip().startswith("INPUT_ISSUE:"), f"unsafe interpolation: {ln!r}"
