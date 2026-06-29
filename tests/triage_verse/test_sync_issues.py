import json

from triage_verse import db
from triage_verse.sync import parse_issue_node, sync_issues


def _node(number, updated, state="OPEN", **over):
    node = {
        "number": number,
        "title": f"issue {number}",
        "body": "text",
        "state": state,
        "stateReason": "NOT_PLANNED" if state == "CLOSED" else None,
        "author": {"login": "alice"},
        "labels": {"nodes": [{"name": "bug"}]},
        "assignees": {"nodes": []},
        "milestone": None,
        "comments": {"totalCount": 2},
        "reactions": {"totalCount": 5},
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": updated,
        "closedAt": "2024-06-01T00:00:00Z" if state == "CLOSED" else None,
    }
    node.update(over)
    return node


def _page(nodes, has_next=False, end_cursor=None):
    return {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                "nodes": nodes,
            }
        }
    }


def test_parse_issue_node_maps_fields():
    row = parse_issue_node("rstudio/shiny", _node(5, "2026-01-02T00:00:00Z"))

    assert row["repo"] == "rstudio/shiny"
    assert row["number"] == 5
    assert row["state"] == "OPEN"
    assert row["author"] == "alice"
    assert json.loads(row["labels_json"]) == ["bug"]
    assert row["comment_count"] == 2
    assert row["reaction_count"] == 5
    assert row["is_pr"] == 0


def test_parse_issue_node_handles_deleted_author():
    row = parse_issue_node("r/r", _node(1, "2026-01-01T00:00:00Z", author=None))
    assert row["author"] is None


def test_full_sync_walks_all_pages_and_sets_cursor(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    pages = [
        _page(
            [_node(3, "2026-06-03T00:00:00Z"), _node(2, "2026-06-02T00:00:00Z")],
            has_next=True,
            end_cursor="c1",
        ),
        _page([_node(1, "2026-06-01T00:00:00Z")]),
    ]
    calls = []

    def fake_graphql(query, variables):
        calls.append(variables)
        return pages[len(calls) - 1]

    count = sync_issues(con, "rstudio/shiny", graphql=fake_graphql, full=True)

    assert count == 3
    assert con.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 3
    assert db.get_cursor(con, "rstudio/shiny", "issues") == "2026-06-03T00:00:00Z"
    assert calls[1]["after"] == "c1"


def test_incremental_sync_stops_at_cursor(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.set_cursor(con, "rstudio/shiny", "issues", "2026-06-02T00:00:00Z")
    pages = [
        _page(
            [
                _node(3, "2026-06-03T00:00:00Z"),
                _node(2, "2026-06-02T00:00:00Z"),  # == cursor: still upserted
                _node(1, "2026-06-01T00:00:00Z"),
            ],  # < cursor: stop, not upserted
            has_next=True,
            end_cursor="c1",
        ),
    ]

    count = sync_issues(con, "rstudio/shiny", graphql=lambda q, v: pages[0], full=False)

    assert count == 2
    numbers = {r["number"] for r in con.execute("SELECT number FROM issues")}
    assert numbers == {2, 3}
    assert db.get_cursor(con, "rstudio/shiny", "issues") == "2026-06-03T00:00:00Z"


def test_empty_repo_sets_no_cursor(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")

    count = sync_issues(con, "rstudio/shiny", graphql=lambda q, v: _page([]), full=True)

    assert count == 0
    assert con.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    assert db.get_cursor(con, "rstudio/shiny", "issues") is None


def test_full_sync_ignores_existing_cursor(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.set_cursor(con, "rstudio/shiny", "issues", "2026-06-02T12:00:00Z")

    count = sync_issues(
        con,
        "rstudio/shiny",
        graphql=lambda q, v: _page(
            [
                _node(3, "2026-06-03T00:00:00Z"),
                _node(1, "2026-06-01T00:00:00Z"),
            ]
        ),
        full=True,
    )

    assert count == 2
    assert db.get_cursor(con, "rstudio/shiny", "issues") == "2026-06-03T00:00:00Z"
