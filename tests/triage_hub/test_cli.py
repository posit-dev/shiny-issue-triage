from triage_hub import cli, db
from triage_hub import sync as sync_mod


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
