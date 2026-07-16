# tests/triage_verse/test_review_app_tier2.py
"""The review app's Tier-2 label helper."""

import importlib.util
import pathlib

from triage_verse.review_app import app as review_app

_spec = importlib.util.spec_from_file_location(
    "fake_gh", pathlib.Path(__file__).parent / "fake_gh.py"
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
FakeGh = _m.FakeGh


def test_app_tier2_label_calls_request_fix(gh_relay):
    gh = FakeGh(
        {
            ("o/r", 5): {
                "labels": [],
                "state": "open",
                "state_reason": None,
                "updated_at": "t",
                "node_id": "N5",
            }
        }
    )
    gh_relay.install(gh)
    review_app.app_tier2_label("o/r", 5, run_gh=gh)
    assert "ai-triage:fix-requested" in gh.issues[("o/r", 5)]["labels"]
