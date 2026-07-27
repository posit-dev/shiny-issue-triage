"""Reconciling mirrored issues that GitHub no longer lists (transferred/deleted)."""

import json

import pytest

from triage_verse import candidates, db, embed, sync

# Captured before the autouse guard below shadows the module attribute, so the
# unit tests for confirm_retirable itself can still reach the real function.
_REAL_CONFIRM = sync.confirm_retirable


@pytest.fixture(autouse=True)
def _no_real_confirmation_reads(monkeypatch):
    """Fail loudly if a test reaches real GitHub to confirm a retirement.

    `reconcile_repo` falls back to `confirm_retirable`, which shells out to `gh`.
    A test that forgets to inject `confirm=` would otherwise quietly depend on
    the network -- and on a nonexistent repo 404ing, which reads as "gone" and
    makes a deletion look confirmed when nothing was actually checked.

    This patches the fallback rather than `gh_run`, because `confirm_retirable`
    binds its `run_gh` default at definition time, whereas `reconcile_repo`
    resolves this name per call.
    """

    def explode(*_args, **_kwargs):
        raise AssertionError(
            "test reached the real gh transport; pass confirm= to "
            "reconcile_repo/sync_issues/sync_all instead"
        )

    monkeypatch.setattr(sync, "confirm_retirable", explode)


def _confirm(verdicts: dict[int, tuple[str, str | None]]):
    """Fake confirmation reader: issue number -> (verdict, detail)."""

    def confirm(repo: str, number: int) -> tuple[str, str | None]:
        return verdicts.get(number, ("live", None))

    return confirm


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

    gone = sync.reconcile_repo(
        con, "r/a", {2}, log=lambda _m: None, confirm=_confirm({1: ("gone", None)})
    )

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

    sync.sync_issues(
        con,
        "r/a",
        graphql=_graphql_returning([2]),
        full=True,
        confirm=_confirm({1: ("gone", None)}),
    )

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

    sync.sync_issues(
        con,
        "r/a",
        graphql=_graphql_returning([2]),
        full=True,
        confirm=_confirm({1: ("gone", None)}),
    )

    after = candidates.candidate_pairs(con, cfg)
    assert not any(("r/a", 1) in ((a[0], a[1]), (b[0], b[1])) for a, b in after)


def test_reembedding_does_not_resurrect_a_retired_issue(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    # Sibling #2 survives, so the walk is non-empty and the empty-response guard
    # stays out of the way; #1 is the retired ghost.
    _insert(con, "r/a", 2)
    embed.embed_repo(con, "r/a", embed.FakeEmbedder())
    sync.sync_issues(
        con,
        "r/a",
        graphql=_graphql_returning([2]),
        full=True,
        confirm=_confirm({1: ("gone", None)}),
    )

    embed.embed_repo(con, "r/a", embed.FakeEmbedder())

    assert db.get_embed_hash(con, "r/a", 1) is None
    assert db.get_embed_hash(con, "r/a", 2) is not None


def test_full_sync_refuses_to_wipe_a_repo_on_an_empty_response(tmp_path):
    """An exception-free zero-node walk means an API or permissions problem, not
    a repo that genuinely lost every issue."""
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    sync.sync_issues(
        con,
        "r/a",
        graphql=_graphql_returning([]),
        full=True,
        confirm=_confirm({}),
    )

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


def test_full_sync_keeps_a_live_issue_the_walk_skipped(tmp_path):
    """Pagination over UPDATED_AT DESC can skip a live issue commented on mid-walk.

    A short walk no longer vetoes the whole repo -- the candidate is confirmed
    against GitHub instead, and a live answer keeps the row.
    """
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    lines: list[str] = []

    sync.sync_issues(
        con,
        "r/a",
        graphql=_graphql_returning([2], total=2),
        full=True,
        log=lines.append,
        confirm=_confirm({1: ("live", None)}),
    )

    assert db.get_issue(con, "r/a", 1) is not None
    assert db.get_issue(con, "r/a", 2) is not None
    assert any("GitHub reports 2" in line for line in lines)
    assert any("still live" in line for line in lines)
    assert not any("REFUSING" in line for line in lines)


def test_full_sync_retires_a_candidate_github_confirms_is_gone(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    sync.sync_issues(
        con,
        "r/a",
        graphql=_graphql_returning([2], total=1),
        full=True,
        confirm=_confirm({1: ("gone", None)}),
    )

    assert db.get_issue(con, "r/a", 1) is None
    assert db.get_issue(con, "r/a", 2) is not None


def test_sync_all_reports_retired_rows_in_its_totals(tmp_path, monkeypatch):
    """The destructive step must be visible to --json / the runs table."""
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    _stub_sync_all_deps(monkeypatch, _graphql_returning([2], total=1))

    totals = sync.sync_all(
        con,
        ["r/a"],
        full=True,
        log=lambda _: None,
        confirm=_confirm({1: ("gone", None)}),
    )

    assert totals["retired"] == 1
    assert db.get_issue(con, "r/a", 1) is None


def test_sync_all_reports_zero_retired_when_nothing_is_deleted(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _stub_sync_all_deps(monkeypatch, _graphql_returning([1]))

    totals = sync.sync_all(
        con,
        ["r/a"],
        full=True,
        log=lambda _: None,
        confirm=_confirm({1: ("gone", None)}),
    )

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


# --- per-candidate confirmation: retire only what GitHub confirms -------------


def test_candidate_github_reports_gone_is_retired(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    lines: list[str] = []

    gone = sync.reconcile_repo(
        con, "r/a", {2}, log=lines.append, confirm=_confirm({1: ("gone", None)})
    )

    assert gone == [1]
    assert db.get_issue(con, "r/a", 1) is None
    assert any("no longer exists" in line for line in lines)


def test_transferred_candidate_is_retired_and_names_its_destination(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    lines: list[str] = []

    gone = sync.reconcile_repo(
        con,
        "r/a",
        {2},
        log=lines.append,
        confirm=_confirm({1: ("transferred", "other/repo")}),
    )

    assert gone == [1]
    assert db.get_issue(con, "r/a", 1) is None
    assert any("transferred to other/repo" in line for line in lines)


def test_a_live_candidate_the_walk_missed_is_kept(tmp_path):
    """The reason this whole mechanism exists."""
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    lines: list[str] = []

    gone = sync.reconcile_repo(
        con, "r/a", {2}, log=lines.append, confirm=_confirm({1: ("live", None)})
    )

    assert gone == []
    assert db.get_issue(con, "r/a", 1) is not None
    assert any("still live" in line for line in lines)


def test_a_candidate_whose_confirmation_read_fails_is_kept(tmp_path):
    """A rate limit or bad gateway must never be read as absence."""
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    lines: list[str] = []

    gone = sync.reconcile_repo(
        con,
        "r/a",
        {2},
        log=lines.append,
        confirm=_confirm({1: ("unknown", "HTTP 502")}),
    )

    assert gone == []
    assert db.get_issue(con, "r/a", 1) is not None
    assert any("could not confirm" in line and "502" in line for line in lines)


def test_implausible_candidate_share_refuses_the_whole_repo(tmp_path):
    """Losing more than a tenth of a repo at once is a symptom of a bad walk."""
    con = db.connect(tmp_path / "m.sqlite")
    for n in range(1, 101):
        _insert(con, "r/a", n)
    lines: list[str] = []
    calls: list[int] = []

    def confirm(repo: str, number: int):
        calls.append(number)
        return ("gone", None)

    gone = sync.reconcile_repo(con, "r/a", {1, 2, 3}, log=lines.append, confirm=confirm)

    assert gone == []
    assert calls == []  # refused before spending a single confirmation read
    assert db.get_issue(con, "r/a", 50) is not None
    assert any("REFUSING" in line for line in lines)


def test_a_small_repo_can_still_retire_despite_the_percentage_cap(tmp_path):
    """A strict 10% cap alone would make retirement impossible on a tiny repo,
    hence RETIRE_MIN_ALLOWANCE."""
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    gone = sync.reconcile_repo(
        con, "r/a", {2}, log=lambda _m: None, confirm=_confirm({1: ("gone", None)})
    )

    assert gone == [1]


def test_reconcile_summarises_candidates_checked_and_retired(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    for n in (1, 2, 3):
        _insert(con, "r/a", n)
    lines: list[str] = []

    sync.reconcile_repo(
        con,
        "r/a",
        {3},
        log=lines.append,
        confirm=_confirm({1: ("gone", None), 2: ("live", None)}),
    )

    assert any("2 candidate(s) checked, 1 retired" in line for line in lines)


# --- confirm_retirable: interpreting GitHub's answer --------------------------


def test_confirm_retirable_reads_404_as_gone():
    from triage_verse import gh

    def run_gh(args, **kwargs):
        raise gh.GhError("gh: Not Found (HTTP 404)")

    assert _REAL_CONFIRM("r/a", 1, run_gh=run_gh) == ("gone", None)


def test_confirm_retirable_reads_a_cross_repo_redirect_as_transferred():
    def run_gh(args, **kwargs):
        return json.dumps(
            {"number": 3902, "repository_url": "https://api.github.com/repos/r/b"}
        )

    assert _REAL_CONFIRM("r/a", 1, run_gh=run_gh) == ("transferred", "r/b")


def test_confirm_retirable_reads_a_same_repo_response_as_live():
    def run_gh(args, **kwargs):
        return json.dumps(
            {"number": 1, "repository_url": "https://api.github.com/repos/r/a"}
        )

    assert _REAL_CONFIRM("r/a", 1, run_gh=run_gh) == ("live", None)


def test_confirm_retirable_is_case_insensitive_about_the_repo():
    def run_gh(args, **kwargs):
        return json.dumps(
            {"number": 1, "repository_url": "https://api.github.com/repos/R/A"}
        )

    assert _REAL_CONFIRM("r/a", 1, run_gh=run_gh) == ("live", None)


def test_confirm_retirable_treats_a_transport_error_as_unknown():
    from triage_verse import gh

    def run_gh(args, **kwargs):
        raise gh.GhError("HTTP 502 Bad Gateway")

    verdict, detail = _REAL_CONFIRM("r/a", 1, run_gh=run_gh)
    assert verdict == "unknown" and "502" in (detail or "")


def test_confirm_retirable_treats_unparseable_json_as_unknown():
    def run_gh(args, **kwargs):
        return "not json{"

    assert _REAL_CONFIRM("r/a", 1, run_gh=run_gh)[0] == "unknown"


def test_confirm_retirable_asks_only_for_that_one_issue():
    """A bounded REST GET, which the egress guard allows as a read."""
    seen: list[list[str]] = []

    def run_gh(args, **kwargs):
        seen.append(args)
        return json.dumps({"repository_url": "https://api.github.com/repos/r/a"})

    _REAL_CONFIRM("r/a", 7, run_gh=run_gh)
    assert seen == [["api", "repos/r/a/issues/7"]]
