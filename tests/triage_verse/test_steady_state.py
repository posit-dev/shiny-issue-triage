"""Stage orchestration for the steady-state loop."""

from triage_verse import steady_state


def test_runs_all_stages_in_order():
    calls = []
    stages = [("a", lambda: calls.append("a")), ("b", lambda: calls.append("b"))]
    res = steady_state.run(stages, log=lambda *a: None)
    assert calls == ["a", "b"]
    assert res["completed"] == ["a", "b"]
    assert res["failed"] is None


def test_stops_at_failing_stage_but_keeps_completed():
    calls = []

    def boom():
        raise RuntimeError("kaboom")

    stages = [
        ("a", lambda: calls.append("a")),
        ("b", boom),
        ("c", lambda: calls.append("c")),
    ]
    res = steady_state.run(stages, log=lambda *a: None)
    assert calls == ["a"]
    assert res["completed"] == ["a"]
    assert res["failed"] == "b"
    assert "kaboom" in res["error"]


def test_steady_state_cli_dry_run(monkeypatch, capsys, tmp_path):
    from triage_verse import cli
    monkeypatch.setenv("TRIAGE_VERSE_DB", str(tmp_path / "m.sqlite"))
    rc = cli.main(["steady-state", "--dry-run", "--config", "config/repos.yaml"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would run: sync" in out and "would run: tier1" in out
