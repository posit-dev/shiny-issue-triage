import pathlib

import pytest

from triage_verse.config import Repo, load_repos

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_load_repos_parses_owner_and_name(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n  - posit-dev/py-shiny\n")

    repos = load_repos(cfg)

    assert repos == [Repo("rstudio", "shiny"), Repo("posit-dev", "py-shiny")]
    assert repos[0].full == "rstudio/shiny"


def test_load_repos_rejects_malformed_entry(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - not-a-repo\n")

    with pytest.raises(ValueError, match="not-a-repo"):
        load_repos(cfg)


def test_checked_in_config_is_pilot_trio():
    repos = load_repos(REPO_ROOT / "config" / "repos.yaml")

    fulls = [r.full for r in repos]
    assert len(fulls) == len(set(fulls))
    assert fulls == ["rstudio/reactlog", "rstudio/shinytest2", "posit-dev/py-shinylive"]


def test_checked_in_config_keeps_fleet_ready_to_uncomment():
    text = (REPO_ROOT / "config" / "repos.yaml").read_text(encoding="utf-8")
    assert "# - rstudio/shiny\n" in text
    assert "# - posit-dev/py-shiny\n" in text


def test_load_repos_rejects_non_list_repositories(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  rstudio/shiny: true\n")

    with pytest.raises(ValueError, match="must be a list"):
        load_repos(cfg)
