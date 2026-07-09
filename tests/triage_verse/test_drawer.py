from triage_verse import db, drawer


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


def _comment_row(**overrides):
    row = {
        "repo": "rstudio/shiny",
        "issue_number": 1,
        "comment_id": 1,
        "author": "bob",
        "body": "hi",
        "created_at": "2024-01-03T00:00:00Z",
        "updated_at": "2024-01-03T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_load_item_full_issue(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(
        con,
        _issue_row(
            labels_json='["bug", "P1"]',
            assignees_json='["carol"]',
            comment_count=2,
        ),
    )
    db.upsert_comment(
        con, _comment_row(comment_id=2, created_at="2024-01-05T00:00:00Z", body="later")
    )
    db.upsert_comment(con, _comment_row(comment_id=1, body="earlier"))

    item = drawer.load_item(con, "rstudio/shiny", 1)
    assert item["title"] == "first"
    assert item["labels"] == ["bug", "P1"]
    assert item["assignees"] == ["carol"]
    assert "labels_json" not in item
    assert "assignees_json" not in item
    assert [c["body"] for c in item["comments"]] == ["earlier", "later"]
    assert item["comments"][0] == {
        "author": "bob",
        "body": "earlier",
        "created_at": "2024-01-03T00:00:00Z",
    }
    assert item["pr"] is None
    assert item["github_url"] == "https://github.com/rstudio/shiny/issues/1"


def test_load_item_missing_returns_none(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert drawer.load_item(con, "rstudio/shiny", 999) is None


def test_load_item_pr_with_metadata(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row(number=7, is_pr=1, state="CLOSED"))
    db.upsert_pr(
        con,
        {
            "repo": "rstudio/shiny",
            "number": 7,
            "merged": 1,
            "merged_at": "2024-03-01T00:00:00Z",
            "closing_issue_refs_json": "[3]",
            "head_ref": "fix",
            "base_ref": "main",
        },
    )

    item = drawer.load_item(con, "rstudio/shiny", 7)
    assert item["github_url"] == "https://github.com/rstudio/shiny/pull/7"
    assert item["pr"] == {
        "merged": True,
        "merged_at": "2024-03-01T00:00:00Z",
        "head_ref": "fix",
        "base_ref": "main",
    }


def test_load_item_pr_without_prs_row(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row(number=8, is_pr=1))
    item = drawer.load_item(con, "rstudio/shiny", 8)
    assert item["pr"] is None
    assert item["github_url"] == "https://github.com/rstudio/shiny/pull/8"


def test_load_item_empty_body_and_no_comments(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.upsert_issue(con, _issue_row(body=None))
    item = drawer.load_item(con, "rstudio/shiny", 1)
    assert item["body"] is None
    assert item["comments"] == []
