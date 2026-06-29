from triage_verse import db
from triage_verse.sync import parse_comment, sync_comments


def _item(comment_id, issue_number, updated, body="hello"):
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": "dana"},
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": updated,
        "issue_url": f"https://api.github.com/repos/rstudio/shiny/issues/{issue_number}",
    }


def test_parse_comment_extracts_issue_number():
    row = parse_comment("rstudio/shiny", _item(11, 123, "2026-06-01T00:00:00Z"))
    assert row["issue_number"] == 123
    assert row["comment_id"] == 11
    assert row["author"] == "dana"


def test_sync_comments_pages_until_short_page(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    page1 = [_item(i, 1, f"2026-06-01T00:00:{i:02d}Z") for i in range(100)]
    page2 = [_item(100, 2, "2026-06-02T00:00:00Z")]
    calls = []

    def fake_api(args):
        assert args[0] == "api"
        calls.append(args[1])
        return page1 if "&page=1" in args[1] else page2

    count = sync_comments(con, "rstudio/shiny", api=fake_api, full=True)

    assert count == 101
    assert len(calls) == 2
    assert "sort=updated" in calls[0] and "direction=asc" in calls[0]
    assert db.get_cursor(con, "rstudio/shiny", "comments") == "2026-06-02T00:00:00Z"


def test_incremental_sync_passes_since(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.set_cursor(con, "rstudio/shiny", "comments", "2026-06-05T00:00:00Z")
    seen = []

    def fake_api(args):
        seen.append(args[1])
        return []

    sync_comments(con, "rstudio/shiny", api=fake_api)

    assert "since=2026-06-05T00:00:00Z" in seen[0]


def test_comment_on_old_issue_is_recaptured(tmp_path):
    """Spec-mandated: issue created long before the cursor gets a new comment."""
    con = db.connect(tmp_path / "m.sqlite")
    con.execute(
        "INSERT INTO issues (repo, number, title, state, created_at, updated_at)"
        " VALUES ('rstudio/shiny', 50, 'ancient', 'OPEN',"
        " '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')")
    db.set_cursor(con, "rstudio/shiny", "comments", "2026-06-01T00:00:00Z")

    new_comment = _item(99, 50, "2026-06-10T00:00:00Z", body="still broken!")
    sync_comments(con, "rstudio/shiny", api=lambda args: [new_comment])

    row = con.execute(
        "SELECT * FROM comments WHERE issue_number=50").fetchone()
    assert row["body"] == "still broken!"
    assert db.get_cursor(con, "rstudio/shiny", "comments") == "2026-06-10T00:00:00Z"


def test_edited_comment_is_upserted_not_duplicated(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    original = _item(7, 1, "2026-06-01T00:00:00Z", body="first")
    sync_comments(con, "rstudio/shiny", api=lambda args: [original], full=True)

    edited = _item(7, 1, "2026-06-02T00:00:00Z", body="edited")
    sync_comments(con, "rstudio/shiny", api=lambda args: [edited])

    rows = con.execute("SELECT body FROM comments").fetchall()
    assert [r["body"] for r in rows] == ["edited"]
    assert db.get_cursor(con, "rstudio/shiny", "comments") == "2026-06-02T00:00:00Z"
