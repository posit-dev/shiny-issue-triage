"""Reprex label request + allowlist guard."""

import importlib.util
import pathlib

import yaml

from triage_verse import reprex

LABELS = (
    pathlib.Path(__file__).resolve().parents[2] / ".github" / "triage" / "labels.yaml"
)

_spec = importlib.util.spec_from_file_location(
    "fake_gh", pathlib.Path(__file__).parent / "fake_gh.py"
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
FakeGh = _m.FakeGh


def test_request_reprex_adds_label_via_graphql(gh_relay):
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
    reprex.request_reprex("o/r", 7, run_gh=gh)
    assert "ai-triage:needs-reprex" in gh.issues[("o/r", 7)]["labels"]
    assert gh.mutating_calls  # a GraphQL mutation was dispatched


def test_marker_label_not_in_allowed_safe_output():
    doc = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    assert "ai-triage:needs-reprex" not in doc.get("allowed_safe_output_labels", [])


def test_marker_labels_are_defined_in_workflow_section():
    doc = yaml.safe_load(LABELS.read_text(encoding="utf-8"))
    names = {e["name"] if isinstance(e, dict) else e for e in doc.get("workflow", [])}
    assert "ai-triage:needs-reprex" in names
    assert "ai-triage:no-reprex" in names
