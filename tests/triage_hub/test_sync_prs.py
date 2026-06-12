import json

from triage_hub import db
from triage_hub.sync import parse_pr_node, sync_prs


def _pr_node(number, updated, **over):
    node = {
        "number": number,
        "title": f"pr {number}",
        "body": "fix",
        "state": "MERGED",
        "author": {"login": "carol"},
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "milestone": None,
        "comments": {"totalCount": 1},
        "createdAt": "2024-05-01T00:00:00Z",
        "updatedAt": updated,
        "closedAt": "2024-05-02T00:00:00Z",
        "merged": True,
        "mergedAt": "2024-05-02T00:00:00Z",
        "headRefName": "fix-thing",
        "baseRefName": "main",
        "closingIssuesReferences": {"nodes": [{"number": 9}]},
    }
    node.update(over)
    return node


def test_parse_pr_node_maps_pr_fields():
    issue_row, pr_row = parse_pr_node("rstudio/shiny",
                                      _pr_node(7, "2026-06-01T00:00:00Z"))

    assert issue_row["is_pr"] == 1
    assert issue_row["state"] == "MERGED"
    assert issue_row["reaction_count"] == 0
    assert pr_row["merged"] == 1
    assert json.loads(pr_row["closing_issue_refs_json"]) == [9]
    assert pr_row["head_ref"] == "fix-thing"


def test_sync_prs_upserts_both_tables(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    page = {"repository": {"pullRequests": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [_pr_node(7, "2026-06-01T00:00:00Z")],
    }}}

    count = sync_prs(con, "rstudio/shiny", graphql=lambda q, v: page, full=True)

    assert count == 1
    assert con.execute(
        "SELECT COUNT(*) FROM issues WHERE is_pr=1").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM prs").fetchone()[0] == 1
    assert db.get_cursor(con, "rstudio/shiny", "prs") == "2026-06-01T00:00:00Z"
