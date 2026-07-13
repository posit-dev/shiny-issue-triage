"""Tests for executor mutation planning and allowlist validation."""

import pathlib

import pytest

from triage_verse import executor, templates

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TMPL = templates.load(REPO_ROOT / "config" / "templates")
ALLOWED = {
    "regression",
    "duplicate",
    "Priority: Critical",
    "Priority: High",
    "Priority: Low",
}


def _decision(action, params, repo="o/r", issue=7):
    return {
        "id": "d1",
        "proposal_id": "p1",
        "repo": repo,
        "issue": issue,
        "action": action,
        "params": params,
        "verdict": "approved",
        "decided_at": "2026-07-13T00:00:00Z",
    }


def _issue(labels=(), state="open"):
    return {
        "updated_at": "2026-07-01T00:00:00Z",
        "node_id": "NID",
        "state": state,
        "state_reason": None,
        "labels": [{"name": name} for name in labels],
    }


@pytest.mark.parametrize(
    "text,expected",
    [
        ("o/r#12", ("o/r", 12)),
        ("other/repo#3", ("other/repo", 3)),
        ("https://github.com/o/r/issues/12", ("o/r", 12)),
        ("#12", ("o/r", 12)),
        ("12", ("o/r", 12)),
        ("nonsense", None),
    ],
)
def test_parse_issue_ref(text, expected):
    assert executor.parse_issue_ref(text, "o/r") == expected


def test_add_label_plans_single_mutation():
    muts, err = executor.plan_decision(
        _decision("add-label", {"label": "regression"}),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert muts == [{"kind": "add-label", "label": "regression"}]


def test_add_label_rejects_unlisted_label():
    muts, err = executor.plan_decision(
        _decision("add-label", {"label": "evil"}), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and err is not None and "evil" in err


def test_set_priority_swaps_existing_priority_labels():
    muts, err = executor.plan_decision(
        _decision("set-priority", {"priority": "High"}),
        _issue(labels=["Priority: Low", "bug"]),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert muts == [
        {"kind": "remove-label", "label": "Priority: Low"},
        {"kind": "add-label", "label": "Priority: High"},
    ]


def test_set_priority_rejects_unknown_value():
    muts, err = executor.plan_decision(
        _decision("set-priority", {"priority": "Urgent"}),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert muts == [] and err is not None


@pytest.mark.parametrize(
    "reason,gh_reason,template_word",
    [
        ("fixed", "completed", "resolved"),
        ("answered", "completed", "resolved"),
        ("stale", "not planned", "not planned"),
        ("not-planned", "not planned", "not planned"),
    ],
)
def test_close_maps_reason_and_comments(reason, gh_reason, template_word):
    muts, err = executor.plan_decision(
        _decision("close", {"reason": reason}), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert err is None
    assert muts[0]["kind"] == "comment" and template_word in muts[0]["body"]
    assert muts[1] == {"kind": "close", "reason": gh_reason}


def test_close_reason_duplicate_is_an_error():
    muts, err = executor.plan_decision(
        _decision("close", {"reason": "duplicate"}), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and "close-duplicate" in err


def test_close_duplicate_same_repo():
    muts, err = executor.plan_decision(
        _decision("close-duplicate", {"canonical": "o/r#3", "cross_repo_option": None}),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert muts[0]["kind"] == "comment"
    assert "https://github.com/o/r/issues/3" in muts[0]["body"]
    assert muts[1] == {"kind": "close-duplicate", "canonical": ["o/r", 3]}


def test_close_duplicate_cross_repo_falls_back_to_not_planned():
    muts, err = executor.plan_decision(
        _decision(
            "close-duplicate",
            {"canonical": "other/repo#3", "cross_repo_option": "close-and-link"},
        ),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert "different repository" in muts[0]["body"]
    assert muts[1] == {"kind": "close", "reason": "not planned"}


@pytest.mark.parametrize(
    "params",
    [
        {"canonical": None, "cross_repo_option": None},
        {"canonical": "nonsense", "cross_repo_option": None},
        {"canonical": "o/r#7", "cross_repo_option": None},  # canonical == self
    ],
)
def test_close_duplicate_bad_canonical_is_an_error(params):
    muts, err = executor.plan_decision(
        _decision("close-duplicate", params), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and err is not None


def test_unknown_action_is_an_error():
    muts, err = executor.plan_decision(
        _decision("transfer", {}), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and "transfer" in err
