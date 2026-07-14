"""Push/pull round-trip for the triage-state bus against a local bare repo."""

import json
import pathlib
import subprocess

from triage_verse import db, state


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def _run_git_factory(default_cwd):
    def run_git(args, *, cwd=None):
        return _git(args, cwd or default_cwd)
    return run_git


def _init_remote(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "triage-state", "."], remote)
    return remote


def _seed_data(data_dir, sub, year_week, records):
    d = data_dir / sub / "2026"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{year_week}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def test_push_then_pull_round_trips_records(tmp_path):
    remote = _init_remote(tmp_path)
    con = db.connect(":memory:")
    con.execute("INSERT INTO repos (repo, issues_cursor) VALUES ('o/r', 'CUR1')")

    data_a = tmp_path / "a"
    _seed_data(data_a, "proposals", "W01", [{"id": "p1"}])
    work_a = tmp_path / "work_a"
    run_git_a = _run_git_factory(str(work_a))
    # clone points at the bare remote
    _git(["clone", str(remote), str(work_a)], tmp_path)
    res = state.push(
        con, ["o/r"], data_dir=data_a, work_dir=work_a, run_git=run_git_a,
        now="2026-07-13T00:00:00Z",
    )
    assert res["pushed"] is True

    # second machine pulls
    data_b = tmp_path / "b"
    data_b.mkdir()
    work_b = tmp_path / "work_b"
    _git(["clone", str(remote), str(work_b)], tmp_path)
    run_git_b = _run_git_factory(str(work_b))
    state.pull(data_dir=data_b, work_dir=work_b, run_git=run_git_b)
    got = (data_b / "proposals" / "2026" / "W01.jsonl").read_text(encoding="utf-8")
    assert json.loads(got.strip())["id"] == "p1"
    cursors = json.loads((data_b / "cursors.json").read_text(encoding="utf-8"))
    assert cursors["repos"]["o/r"]["issues"] == "CUR1"


def test_push_with_no_changes_makes_no_commit(tmp_path):
    remote = _init_remote(tmp_path)
    con = db.connect(":memory:")
    data = tmp_path / "d"
    _seed_data(data, "decisions", "W01", [{"id": "d1"}])
    work = tmp_path / "work"
    _git(["clone", str(remote), str(work)], tmp_path)
    rg = _run_git_factory(str(work))
    state.push(con, [], data_dir=data, work_dir=work, run_git=rg, now="2026-07-13T00:00:00Z")
    before = _git(["rev-list", "--count", "HEAD"], work).strip()
    res = state.push(con, [], data_dir=data, work_dir=work, run_git=rg, now="2026-07-13T00:00:00Z")
    after = _git(["rev-list", "--count", "HEAD"], work).strip()
    assert res["pushed"] is False
    assert before == after


def test_export_cursors_shape():
    con = db.connect(":memory:")
    con.execute(
        "INSERT INTO repos (repo, issues_cursor, prs_cursor, comments_cursor)"
        " VALUES ('o/r', 'I', 'P', 'C')"
    )
    out = state.export_cursors(con, ["o/r"], now="2026-07-13T00:00:00Z")
    assert out == {
        "exported_at": "2026-07-13T00:00:00Z",
        "repos": {"o/r": {"issues": "I", "prs": "P", "comments": "C"}},
    }


def test_state_cli_parses():
    from triage_verse import cli
    args = cli.build_parser().parse_args(["state", "push"])
    assert args.func is not None
