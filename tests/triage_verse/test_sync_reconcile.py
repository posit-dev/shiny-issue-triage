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


def _graphql_returning(numbers, total=None):
    """A one-page issues walk. `total` is GitHub's reported totalCount; it
    defaults to the node count (a complete, un-raced walk)."""

    def graphql(query, variables):
        return {
            "repository": {
                "issues": {
                    "totalCount": len(numbers) if total is None else total,
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
    # A surviving sibling in r/a, so the walk below returns a non-empty node set
    # and the empty-response guard does not engage; r/a#1 is the only absentee.
    _insert(con, "r/a", 2, title="unrelated thing", body="unrelated thing")
    _insert(con, "r/b", 9, title="crash on init", body="stack trace")
    embedder = embed.FakeEmbedder()
    embed.embed_repo(con, "r/a", embedder)
    embed.embed_repo(con, "r/b", embedder)
    cfg = types.SimpleNamespace(cosine_threshold=0.8, candidate_top_k=10)

    before = candidates.candidate_pairs(con, cfg)
    assert any(
        {(a[0], a[1]), (b[0], b[1])} == {("r/a", 1), ("r/b", 9)} for a, b in before
    )

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([2]), full=True)

    after = candidates.candidate_pairs(con, cfg)
    assert not any(("r/a", 1) in ((a[0], a[1]), (b[0], b[1])) for a, b in after)


def test_reembedding_does_not_resurrect_a_retired_issue(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    # Sibling #2 survives, so the walk is non-empty and the empty-response guard
    # stays out of the way; #1 is the retired ghost.
    _insert(con, "r/a", 2)
    embed.embed_repo(con, "r/a", embed.FakeEmbedder())
    sync.sync_issues(con, "r/a", graphql=_graphql_returning([2]), full=True)

    embed.embed_repo(con, "r/a", embed.FakeEmbedder())

    assert db.get_embed_hash(con, "r/a", 1) is None
    assert db.get_embed_hash(con, "r/a", 2) is not None


def test_full_sync_refuses_to_wipe_a_repo_on_an_empty_response(tmp_path):
    """An exception-free zero-node walk means an API or permissions problem, not
    a repo that genuinely lost every issue."""
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([]), full=True)

    assert db.get_issue(con, "r/a", 1) is not None
    assert db.get_issue(con, "r/a", 2) is not None


def _stub_sync_all_deps(monkeypatch, graphql):
    """Run sync_all's issue walk against `graphql`, with PRs and comments stubbed.

    `sync_issues` binds gh_graphql as a default argument, so the fake is injected
    by wrapping the function rather than patching the module attribute.
    """
    real_sync_issues = sync.sync_issues
    monkeypatch.setattr(
        sync,
        "sync_issues",
        lambda con, repo, **kw: real_sync_issues(con, repo, graphql=graphql, **kw),
    )
    monkeypatch.setattr(sync, "sync_prs", lambda *a, **k: 0)
    monkeypatch.setattr(sync, "sync_comments", lambda *a, **k: 0)


def test_full_sync_refuses_to_retire_when_the_walk_saw_fewer_than_github_reports(
    tmp_path,
):
    """Pagination over UPDATED_AT DESC can skip a live issue that is commented on
    mid-walk; a short walk must delete nothing rather than retire it."""
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    lines: list[str] = []

    # GitHub reports 2 issues but the walk only returned #2: #1 was skipped, not
    # removed.
    sync.sync_issues(
        con,
        "r/a",
        graphql=_graphql_returning([2], total=2),
        full=True,
        log=lines.append,
    )

    assert db.get_issue(con, "r/a", 1) is not None
    assert db.get_issue(con, "r/a", 2) is not None
    assert any("REFUSING" in line and "GitHub reports 2" in line for line in lines)


def test_full_sync_still_retires_when_the_count_matches(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([2], total=1), full=True)

    assert db.get_issue(con, "r/a", 1) is None
    assert db.get_issue(con, "r/a", 2) is not None


def test_sync_all_reports_retired_rows_in_its_totals(tmp_path, monkeypatch):
    """The destructive step must be visible to --json / the runs table."""
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    _stub_sync_all_deps(monkeypatch, _graphql_returning([2], total=1))

    totals = sync.sync_all(con, ["r/a"], full=True, log=lambda _: None)

    assert totals["retired"] == 1
    assert db.get_issue(con, "r/a", 1) is None


def test_sync_all_reports_zero_retired_when_nothing_is_deleted(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _stub_sync_all_deps(monkeypatch, _graphql_returning([1]))

    totals = sync.sync_all(con, ["r/a"], full=True, log=lambda _: None)

    assert totals["retired"] == 0


def test_reconcile_repo_returns_empty_and_warns_on_an_empty_response(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    lines: list[str] = []

    gone = sync.reconcile_repo(con, "r/a", set(), log=lines.append)

    assert gone == []
    assert db.get_issue(con, "r/a", 1) is not None
    assert db.get_issue(con, "r/a", 2) is not None
    assert any("REFUSING" in line for line in lines)


def test_reconcile_repo_allows_a_genuinely_empty_repo(tmp_path):
    """The guard costs nothing legitimately: an empty repo has no mirrored rows."""
    con = db.connect(tmp_path / "m.sqlite")

    assert sync.reconcile_repo(con, "r/a", set(), log=lambda _: None) == []


def test_reconcile_logs_even_when_nothing_is_retired(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    lines: list[str] = []

    sync.sync_issues(
        con, "r/a", graphql=_graphql_returning([1]), full=True, log=lines.append
    )

    assert any("nothing to retire" in line for line in lines)


def test_delete_issue_leaves_pull_requests_alone(tmp_path):
    """`issues` and `prs` share (repo, number), and comments/vectors carry no
    is_pr, so a PR must be a no-op rather than a partial strip."""
    con = db.connect(tmp_path / "m.sqlite")
    con.execute(
        "INSERT INTO issues (repo, number, title, body, state, created_at,"
        " updated_at, is_pr) VALUES ('r/a', 7, 'T', 'B', 'OPEN',"
        " '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z', 1)"
    )
    db.upsert_comment(
        con,
        {
            "repo": "r/a",
            "issue_number": 7,
            "comment_id": 1,
            "author": "x",
            "body": "hi",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    db.upsert_vector(con, "r/a", 7, "h", embed.FakeEmbedder().embed(["T\nB"])[0])
    con.commit()

    db.delete_issue(con, "r/a", 7)

    assert db.get_issue(con, "r/a", 7) is not None
    assert db.get_embed_hash(con, "r/a", 7) == "h"
    assert (
        con.execute(
            "SELECT COUNT(*) FROM comments WHERE repo='r/a' AND issue_number=7"
        ).fetchone()[0]
        == 1
    )
