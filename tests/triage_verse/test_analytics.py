import json

from triage_verse import analytics, db


def _seed(con):
    rows = [
        # (number, created, closed, state, state_reason)
        (1, "2026-01-05T10:00:00Z", None, "OPEN", None),
        (2, "2026-01-06T10:00:00Z", "2026-01-20T10:00:00Z", "CLOSED", "COMPLETED"),
        (3, "2026-01-15T10:00:00Z", "2026-01-21T10:00:00Z", "CLOSED", "NOT_PLANNED"),
        (4, "2026-02-02T10:00:00Z", None, "OPEN", None),
    ]
    for number, created, closed, state, reason in rows:
        con.execute(
            "INSERT INTO issues (repo, number, title, state, state_reason,"
            " created_at, updated_at, closed_at)"
            " VALUES ('rstudio/shiny', ?, 't', ?, ?, ?, ?, ?)",
            (number, state, reason, created, created, closed))
    con.commit()


def test_weekly_open_counts_sweeps_history(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)

    series = analytics.weekly_open_counts(con, as_of="2026-02-09T00:00:00Z")

    by_week = {p["week"]: p["open"] for p in series}
    # Monday 2026-01-12: issues 1 and 2 created, none closed yet -> 2 open
    assert by_week["2026-W03"] == 2
    # Monday 2026-01-26: issues 2 and 3 closed -> only issue 1 open
    assert by_week["2026-W05"] == 1
    # Monday 2026-02-09: issue 4 also open -> 2 open
    assert by_week["2026-W07"] == 2


def test_weekly_flux_counts_opened_and_closed(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)

    flux = analytics.weekly_flux(con)

    by_week = {f["week"]: f for f in flux}
    assert by_week["2026-W02"]["opened"] == 2
    assert by_week["2026-W04"]["closed"] == 2


def test_close_reason_mix(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)

    mix = analytics.close_reason_mix(con)

    assert mix == {"COMPLETED": 1, "NOT_PLANNED": 1}


def test_export_writes_json(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)
    out = tmp_path / "analytics.json"

    analytics.export(con, out)

    data = json.loads(out.read_text())
    assert "generated_at" in data
    assert "rstudio/shiny" in data["repos"]
    repo_block = data["repos"]["rstudio/shiny"]
    assert {"weekly_open", "weekly_flux", "close_reasons"} <= set(repo_block)


def test_weekly_open_counts_first_entry_is_zero(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed(con)

    series = analytics.weekly_open_counts(con, as_of="2026-02-09T00:00:00Z")

    # Start-of-Monday sampling: the first week (before any issue's creation
    # instant) shows 0 open. Locks in the documented semantics.
    assert series[0]["open"] == 0


def test_empty_db_returns_empty_analytics(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")

    assert analytics.weekly_open_counts(con) == []
    assert analytics.weekly_flux(con) == []
    assert analytics.close_reason_mix(con) == {}


def test_weekly_flux_same_week_open_and_close(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    con.execute(
        "INSERT INTO issues (repo, number, title, state, state_reason,"
        " created_at, updated_at, closed_at)"
        " VALUES ('rstudio/shiny', 1, 't', 'CLOSED', 'COMPLETED',"
        " '2026-03-02T09:00:00Z', '2026-03-04T09:00:00Z', '2026-03-04T09:00:00Z')")
    con.commit()

    flux = analytics.weekly_flux(con)
    by_week = {f["week"]: f for f in flux}
    # 2026-03-02 and 2026-03-04 are both in ISO week 2026-W10
    assert by_week["2026-W10"]["opened"] == 1
    assert by_week["2026-W10"]["closed"] == 1
