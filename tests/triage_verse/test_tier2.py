"""Tier 2 label request + allowlist guard."""

import pathlib

import yaml

from triage_verse import tier2

LABELS = pathlib.Path(__file__).resolve().parents[2] / ".github" / "triage" / "labels.yaml"


def test_request_fix_adds_label_via_gh():
    calls = []
    tier2.request_fix("o/r", 7, run_gh=lambda args, **k: calls.append(args) or "")
    assert calls == [["issue", "edit", "7", "--repo", "o/r",
                      "--add-label", "ai-triage:fix-requested"]]


def test_marker_label_not_in_allowed_safe_output():
    doc = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    assert "ai-triage:fix-requested" not in doc.get("allowed_safe_output_labels", [])


def test_marker_label_is_defined_in_workflow_section():
    doc = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    names = {e["name"] if isinstance(e, dict) else e for e in doc.get("workflow", [])}
    assert "ai-triage:fix-requested" in names
