"""Tests for executor comment templates."""

import pathlib

import pytest

from triage_verse import templates

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REAL_DIR = REPO_ROOT / "config" / "templates"


def test_load_real_templates_and_render_duplicate():
    loaded = templates.load(REAL_DIR)
    assert set(loaded) == set(templates.TEMPLATE_NAMES)
    body = templates.render(
        loaded, "close-duplicate", canonical_url="https://github.com/o/r/issues/1"
    )
    assert "https://github.com/o/r/issues/1" in body
    assert body.endswith("\n")
    # Every close template invites reopening (community-trust requirement).
    for name in templates.TEMPLATE_NAMES:
        if name.startswith("close-"):
            assert "reopen" in loaded[name]


def test_missing_template_file_raises(tmp_path):
    with pytest.raises(templates.TemplateError, match="missing template"):
        templates.load(tmp_path)


def test_unknown_placeholder_raises(tmp_path):
    for name in templates.TEMPLATE_NAMES:
        (tmp_path / f"{name}.md").write_text("ok", encoding="utf-8")
    (tmp_path / "close-completed.md").write_text("hi {rationale}", encoding="utf-8")
    with pytest.raises(templates.TemplateError, match="unknown placeholder"):
        templates.load(tmp_path)


def test_link_duplicate_template_loads_and_renders():
    import pathlib

    from triage_verse import templates

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    loaded = templates.load(repo_root / "config" / "templates")
    body = templates.render(
        loaded, "link-duplicate", canonical_url="https://github.com/o/r/issues/3"
    )
    assert "https://github.com/o/r/issues/3" in body
    assert "open" in body.lower()
