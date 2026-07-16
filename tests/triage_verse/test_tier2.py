"""Tier 2 label request + allowlist guard."""

import importlib.util
import pathlib

import yaml

from triage_verse import tier2

LABELS = (
    pathlib.Path(__file__).resolve().parents[2] / ".github" / "triage" / "labels.yaml"
)

_spec = importlib.util.spec_from_file_location(
    "fake_gh", pathlib.Path(__file__).parent / "fake_gh.py"
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
FakeGh = _m.FakeGh


def test_request_fix_adds_label_via_graphql(gh_relay):
    gh = FakeGh(
        {
            ("o/r", 7): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": "t",
                "node_id": "N7",
            }
        }
    )
    gh_relay.install(gh)
    tier2.request_fix("o/r", 7, run_gh=gh)
    assert "ai-triage:fix-requested" in gh.issues[("o/r", 7)]["labels"]
    assert gh.mutating_calls  # a GraphQL mutation was dispatched


def test_marker_label_not_in_allowed_safe_output():
    doc = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    assert "ai-triage:fix-requested" not in doc.get("allowed_safe_output_labels", [])


def test_marker_label_is_defined_in_workflow_section():
    doc = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    names = {e["name"] if isinstance(e, dict) else e for e in doc.get("workflow", [])}
    assert "ai-triage:fix-requested" in names
