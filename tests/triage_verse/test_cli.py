from triage_verse import cli, db
from triage_verse import sync as sync_mod


def test_sync_all_records_run_and_counts(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "m.sqlite")
    monkeypatch.setattr(sync_mod, "sync_issues", lambda con, repo, **kw: 2)
    monkeypatch.setattr(sync_mod, "sync_prs", lambda con, repo, **kw: 1)
    monkeypatch.setattr(sync_mod, "sync_comments", lambda con, repo, **kw: 3)

    summary = sync_mod.sync_all(con, ["rstudio/shiny", "rstudio/bslib"],
                                full=False, log=lambda msg: None)

    assert summary == {"repos": 2, "issues": 4, "prs": 2, "comments": 6}
    run = con.execute("SELECT * FROM runs").fetchone()
    assert run["kind"] == "sync"
    assert run["finished_at"] is not None


def test_cli_sync_invokes_sync_all(tmp_path, monkeypatch):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n")
    captured = {}

    def fake_sync_all(con, repos, *, full, log):
        captured["repos"] = repos
        captured["full"] = full
        return {"repos": 1, "issues": 0, "prs": 0, "comments": 0}

    monkeypatch.setattr(sync_mod, "sync_all", fake_sync_all)

    rc = cli.main(["sync", "--db", str(tmp_path / "m.sqlite"),
                   "--config", str(cfg), "--full"])

    assert rc == 0
    assert captured["repos"] == ["rstudio/shiny"]
    assert captured["full"] is True


def test_cli_sync_single_repo_filter(tmp_path, monkeypatch):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n  - rstudio/bslib\n")
    captured = {}

    def fake_sync_all(con, repos, *, full, log):
        captured["repos"] = repos
        return {"repos": 1, "issues": 0, "prs": 0, "comments": 0}

    monkeypatch.setattr(sync_mod, "sync_all", fake_sync_all)

    rc = cli.main(["sync", "--db", str(tmp_path / "m.sqlite"),
                   "--config", str(cfg), "--repo", "rstudio/bslib"])

    assert rc == 0
    assert captured["repos"] == ["rstudio/bslib"]


def test_cli_sync_unknown_repo_returns_1(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n")

    rc = cli.main(["sync", "--db", str(tmp_path / "m.sqlite"),
                   "--config", str(cfg), "--repo", "rstudio/nonexistent"])

    assert rc == 1


def test_sync_all_finishes_run_on_error(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "m.sqlite")
    monkeypatch.setattr(sync_mod, "sync_issues", lambda con, repo, **kw: 1)

    def boom(con, repo, **kw):
        if repo == "rstudio/bslib":
            raise RuntimeError("network died")
        return 0

    monkeypatch.setattr(sync_mod, "sync_prs", boom)
    monkeypatch.setattr(sync_mod, "sync_comments", lambda con, repo, **kw: 0)

    import pytest
    with pytest.raises(RuntimeError, match="network died"):
        sync_mod.sync_all(con, ["rstudio/shiny", "rstudio/bslib"],
                          log=lambda m: None)

    run = con.execute("SELECT * FROM runs").fetchone()
    assert run["finished_at"] is not None
    # rstudio/shiny fully synced before the crash on bslib's PRs
    assert '"issues": 2' in run["summary_json"]
