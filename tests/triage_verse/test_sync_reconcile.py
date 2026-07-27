"""Reconciling mirrored issues that GitHub no longer lists (transferred/deleted)."""

from triage_verse import candidates, db, embed, sync


def _insert(con, repo, number, title="T", body="B", state="OPEN"):
    con.execute(
        "INSERT INTO issues (repo, number, title, body, state, created_at,"
        " updated_at, is_pr) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z',"
        " '2026-06-01T00:00:00Z', 0)",
        (repo, number, title, body, state),
    )
    con.commit()


def test_delete_issue_removes_row_comments_and_vector(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    db.upsert_comment(
        con,
        {
            "repo": "r/a",
            "issue_number": 1,
            "comment_id": 99,
            "author": "x",
            "body": "hi",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    db.upsert_vector(con, "r/a", 1, "h", embed.FakeEmbedder().embed(["T\nB"])[0])
    con.commit()

    db.delete_issue(con, "r/a", 1)

    assert db.get_issue(con, "r/a", 1) is None
    assert db.get_embed_hash(con, "r/a", 1) is None
    assert (
        con.execute(
            "SELECT COUNT(*) FROM comments WHERE repo='r/a' AND issue_number=1"
        ).fetchone()[0]
        == 0
    )
    assert con.execute("SELECT COUNT(*) FROM vec_issues").fetchone()[0] == 0


def test_reconcile_repo_deletes_only_unseen_issues(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    _insert(con, "r/b", 3)

    gone = sync.reconcile_repo(con, "r/a", {2})

    assert gone == [1]
    assert db.get_issue(con, "r/a", 1) is None
    assert db.get_issue(con, "r/a", 2) is not None
    assert db.get_issue(con, "r/b", 3) is not None


def _graphql_returning(numbers):
    def graphql(query, variables):
        return {
            "repository": {
                "issues": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": n,
                            "title": "T",
                            "body": "B",
                            "state": "OPEN",
                            "stateReason": None,
                            "author": {"login": "a"},
                            "labels": {"nodes": []},
                            "assignees": {"nodes": []},
                            "milestone": None,
                            "comments": {"totalCount": 0},
                            "reactions": {"totalCount": 0},
                            "createdAt": "2026-01-01T00:00:00Z",
                            "updatedAt": "2026-06-01T00:00:00Z",
                            "closedAt": None,
                        }
                        for n in numbers
                    ],
                }
            }
        }

    return graphql


def test_full_sync_retires_a_transferred_issue(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([2]), full=True)

    assert db.get_issue(con, "r/a", 1) is None
    assert db.get_issue(con, "r/a", 2) is not None


def test_incremental_sync_never_retires_anything(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([2]), full=False)

    assert db.get_issue(con, "r/a", 1) is not None


def test_retired_ghost_stops_pairing_with_its_transferred_copy(tmp_path):
    """The bug this task exists to prevent: A#1 transferred to r/b#9."""
    import types

    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1, title="crash on init", body="stack trace")
    _insert(con, "r/b", 9, title="crash on init", body="stack trace")
    embedder = embed.FakeEmbedder()
    embed.embed_repo(con, "r/a", embedder)
    embed.embed_repo(con, "r/b", embedder)
    cfg = types.SimpleNamespace(cosine_threshold=0.8, candidate_top_k=10)

    before = candidates.candidate_pairs(con, cfg)
    assert any(
        {(a[0], a[1]), (b[0], b[1])} == {("r/a", 1), ("r/b", 9)} for a, b in before
    )

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([]), full=True)

    after = candidates.candidate_pairs(con, cfg)
    assert not any(("r/a", 1) in ((a[0], a[1]), (b[0], b[1])) for a, b in after)


def test_reembedding_does_not_resurrect_a_retired_issue(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    embed.embed_repo(con, "r/a", embed.FakeEmbedder())
    sync.sync_issues(con, "r/a", graphql=_graphql_returning([]), full=True)

    embed.embed_repo(con, "r/a", embed.FakeEmbedder())

    assert db.get_embed_hash(con, "r/a", 1) is None
