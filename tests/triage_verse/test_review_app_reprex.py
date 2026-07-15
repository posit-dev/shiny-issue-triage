# tests/triage_verse/test_review_app_reprex.py
"""The review app's reprex label helper + keyboard bindings."""

import importlib.util
import pathlib

from triage_verse import review_queue
from triage_verse.review_app import app as review_app

_spec = importlib.util.spec_from_file_location(
    "fake_gh", pathlib.Path(__file__).parent / "fake_gh.py"
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
FakeGh = _m.FakeGh


def test_app_reprex_label_calls_request_reprex(gh_relay):
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
    review_app.app_reprex_label("o/r", 5, run_gh=gh)
    assert "ai-triage:needs-reprex" in gh.issues[("o/r", 5)]["labels"]


def test_enter_toggles_and_arrows_navigate():
    # Enter toggles the drawer rather than always (re)opening it.
    assert review_queue.KEY_ACTIONS["Enter"] == "toggle"
    # Arrow keys mirror j/k selection movement.
    assert review_queue.KEY_ACTIONS["ArrowDown"] == "next"
    assert review_queue.KEY_ACTIONS["ArrowUp"] == "prev"
