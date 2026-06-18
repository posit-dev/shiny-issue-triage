from triage_hub import db, verify


def _seed_open(con, repo, n):
    for i in range(n):
        con.execute(
            "INSERT INTO issues (repo, number, title, state, created_at,"
            " updated_at) VALUES (?, ?, 't', 'OPEN',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (repo, i + 1))
    con.commit()


def test_verify_counts_reports_match_and_mismatch(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_open(con, "rstudio/shiny", 10)
    _seed_open(con, "rstudio/bslib", 4)
    github_counts = {"rstudio/shiny": 10, "rstudio/bslib": 9}

    def fake_api(args):
        assert args[0] == "api"
        for repo, total in github_counts.items():
            if f"repo:{repo}" in args[1]:
                return {"total_count": total}
        raise AssertionError(f"unexpected call: {args}")

    results = verify.verify_counts(con, ["rstudio/shiny", "rstudio/bslib"],
                                   api=fake_api, tolerance=2)

    by_repo = {r["repo"]: r for r in results}
    assert by_repo["rstudio/shiny"]["ok"] is True
    assert by_repo["rstudio/bslib"]["ok"] is False
    assert by_repo["rstudio/bslib"]["mirror"] == 4
    assert by_repo["rstudio/bslib"]["github"] == 9


def test_small_drift_within_tolerance_is_ok(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_open(con, "rstudio/shiny", 10)

    results = verify.verify_counts(
        con, ["rstudio/shiny"],
        api=lambda args: {"total_count": 11}, tolerance=2)

    assert results[0]["ok"] is True
