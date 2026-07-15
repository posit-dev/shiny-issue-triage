# tests/triage_verse/test_review_app_reprex.py
"""The review app's reprex label helper + keyboard bindings."""

from triage_verse import review_queue
from triage_verse.review_app import app as review_app


def test_app_reprex_label_calls_request_reprex():
    calls = []
    review_app.app_reprex_label(
        "o/r", 5, run_gh=lambda args, **k: calls.append(args) or ""
    )
    assert calls[0] == [
        "issue",
        "edit",
        "5",
        "--repo",
        "o/r",
        "--add-label",
        "ai-triage:needs-reprex",
    ]


def test_enter_toggles_and_arrows_navigate():
    # Enter toggles the drawer rather than always (re)opening it.
    assert review_queue.KEY_ACTIONS["Enter"] == "toggle"
    # Arrow keys mirror j/k selection movement.
    assert review_queue.KEY_ACTIONS["ArrowDown"] == "next"
    assert review_queue.KEY_ACTIONS["ArrowUp"] == "prev"
