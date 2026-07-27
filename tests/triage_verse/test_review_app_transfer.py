"""Rendering of link-duplicate and suggest-transfer in the review app."""

from triage_verse.review_app import app


def _proposal(action, canonical="posit-dev/py-shiny#12"):
    return {
        "id": "p1",
        "repo": "rstudio/shiny",
        "issue": 7,
        "action": action,
        "params": {"canonical": canonical, "cross_repo_option": "transfer"},
        "confidence": 0.9,
        "rationale": "belongs in the python package",
        "evidence": [],
    }


def test_params_line_for_suggest_transfer_shows_destination():
    line = app._params_line(_proposal("suggest-transfer"))
    assert "posit-dev/py-shiny" in line
    assert "canonical" not in line


def test_params_line_for_link_duplicate_reads_as_words():
    line = app._params_line(_proposal("link-duplicate"))
    assert "posit-dev/py-shiny#12" in line
    assert not line.startswith("{")


def test_params_line_falls_back_to_params_for_other_actions():
    p = {"action": "add-label", "params": {"label": "regression"}}
    assert "regression" in app._params_line(p)


def test_row_label_uses_the_readable_params_line():
    label = app._row_label(_proposal("suggest-transfer"))
    assert label.startswith("rstudio/shiny#7 — suggest-transfer:")
    assert "posit-dev/py-shiny" in label
    assert "cross_repo_option" not in label


def test_drawer_transfer_states_that_approving_does_not_move_the_issue():
    parts = app._drawer_transfer(_proposal("suggest-transfer"))
    text = " ".join(str(p) for p in parts)
    assert "posit-dev/py-shiny" in text
    assert "wrong location" in text
    assert "Transfers" in text


def test_drawer_transfer_handles_a_missing_destination():
    parts = app._drawer_transfer(_proposal("suggest-transfer", canonical=None))
    text = " ".join(str(p) for p in parts)
    assert "not identified" in text


def test_destination_badge_renders_leftmost_of_the_other_badges():
    """The destination badge must precede the stale and not-now badges.

    The relative order of `stale` and `not now` between themselves is
    pre-existing behaviour of those two blocks and is not asserted here.
    """
    p = _proposal("suggest-transfer")
    p["stale"] = True
    p["deferred"] = True
    html = str(app.row_ui("p1", p, "snippet"))
    i_dest = html.index("posit-dev/py-shiny")
    assert i_dest < html.index("stale")
    assert i_dest < html.index("not now")
