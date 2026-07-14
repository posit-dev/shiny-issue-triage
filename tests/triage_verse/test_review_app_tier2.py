# tests/triage_verse/test_review_app_tier2.py
"""The review app's Tier-2 label helper."""

from triage_verse.review_app import app as review_app


def test_app_tier2_label_calls_request_fix():
    calls = []
    review_app.app_tier2_label(
        "o/r", 5, run_gh=lambda args, **k: calls.append(args) or ""
    )
    assert calls[0] == [
        "issue",
        "edit",
        "5",
        "--repo",
        "o/r",
        "--add-label",
        "ai-triage:fix-requested",
    ]
