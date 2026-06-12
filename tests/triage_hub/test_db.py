from triage_hub import db


def _issue_row(**overrides):
    row = {
        "repo": "rstudio/shiny",
        "number": 1,
        "title": "first",
        "body": "body",
        "state": "OPEN",
        "state_reason": None,
        "author": "alice",
        "labels_json": "[]",
        "assignees_json": "[]",
        "milestone": None,
        "comment_count": 0,
        "reaction_count": 0,
        "is_pr": 0,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "closed_at": None,
    }
    row.update(overrides)
    return row


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "m.sqlite"
    con = db.connect(path)
    con.close()
    con = db.connect(path)  # re-running schema must not fail
    assert con.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0


def test_upsert_issue_twice_updates_in_place(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row())
    db.upsert_issue(con, _issue_row(title="renamed", state="CLOSED",
                                    state_reason="COMPLETED",
                                    closed_at="2024-02-01T00:00:00Z"))

    rows = con.execute("SELECT * FROM issues").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "renamed"
    assert rows[0]["state"] == "CLOSED"
    assert rows[0]["state_reason"] == "COMPLETED"


def test_upsert_pr_and_comment(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row(number=7, is_pr=1))
    db.upsert_pr(con, {"repo": "rstudio/shiny", "number": 7, "merged": 1,
                       "merged_at": "2024-03-01T00:00:00Z",
                       "closing_issue_refs_json": "[3]",
                       "head_ref": "fix", "base_ref": "main"})
    db.upsert_comment(con, {"repo": "rstudio/shiny", "issue_number": 1,
                            "comment_id": 42, "author": "bob", "body": "hi",
                            "created_at": "2024-01-03T00:00:00Z",
                            "updated_at": "2024-01-03T00:00:00Z"})
    db.upsert_comment(con, {"repo": "rstudio/shiny", "issue_number": 1,
                            "comment_id": 42, "author": "bob", "body": "edited",
                            "created_at": "2024-01-03T00:00:00Z",
                            "updated_at": "2024-01-04T00:00:00Z"})

    assert con.execute("SELECT merged FROM prs WHERE number=7").fetchone()[0] == 1
    comments = con.execute("SELECT body FROM comments").fetchall()
    assert [c["body"] for c in comments] == ["edited"]


def test_cursors_roundtrip(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert db.get_cursor(con, "rstudio/shiny", "issues") is None
    db.set_cursor(con, "rstudio/shiny", "issues", "2026-06-01T00:00:00Z")
    db.set_cursor(con, "rstudio/shiny", "comments", "2026-06-02T00:00:00Z")
    assert db.get_cursor(con, "rstudio/shiny", "issues") == "2026-06-01T00:00:00Z"
    assert db.get_cursor(con, "rstudio/shiny", "comments") == "2026-06-02T00:00:00Z"
    db.set_cursor(con, "rstudio/shiny", "issues", "2026-06-03T00:00:00Z")
    assert db.get_cursor(con, "rstudio/shiny", "issues") == "2026-06-03T00:00:00Z"
    assert con.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1


def test_record_run(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    run_id = db.start_run(con, "sync")
    db.finish_run(con, run_id, {"issues": 3})
    row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["kind"] == "sync"
    assert row["finished_at"] is not None
    assert '"issues": 3' in row["summary_json"]
