"""CLI wiring tests for execute/undo (executor functions monkeypatched)."""

from triage_verse import cli, executor


def test_execute_defaults_to_dry_run_and_env_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGE_VERSE_DECISIONS", str(tmp_path / "d"))
    monkeypatch.setenv("TRIAGE_VERSE_DB", str(tmp_path / "m.sqlite"))
    seen = {}

    def fake_execute(con, **kwargs):
        seen.update(kwargs)
        return {
            "batch_id": "b1",
            "counts": {
                "applied": 0,
                "dry-run": 2,
                "stale-needs-rereview": 0,
                "error": 0,
            },
        }

    monkeypatch.setattr(executor, "execute", fake_execute)
    rc = cli.main(["execute"])
    assert rc == 0
    assert seen["apply"] is False
    assert seen["decisions_dir"] == str(tmp_path / "d")
    assert seen["proposals_dir"] == ".data/proposals"
    assert seen["run_gh"] is not None


def test_execute_apply_flag_and_error_exit_code(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGE_VERSE_DB", str(tmp_path / "m.sqlite"))
    seen = {}

    def fake_execute(con, **kwargs):
        seen.update(kwargs)
        return {
            "batch_id": "b1",
            "counts": {
                "applied": 1,
                "dry-run": 0,
                "stale-needs-rereview": 0,
                "error": 1,
            },
        }

    monkeypatch.setattr(executor, "execute", fake_execute)
    rc = cli.main(["execute", "--apply", "--repo", "o/r", "--limit", "5"])
    assert rc == 1
    assert seen["apply"] is True and seen["repo"] == "o/r" and seen["limit"] == 5


def test_undo_requires_batch_and_passes_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("TRIAGE_VERSE_DB", str(tmp_path / "m.sqlite"))
    seen = {}

    def fake_undo(con, **kwargs):
        seen.update(kwargs)
        return {
            "batch_id": "u1",
            "counts": {"applied": 0, "dry-run": 1, "error": 0, "skipped": 0},
        }

    monkeypatch.setattr(executor, "undo", fake_undo)
    rc = cli.main(["undo", "--batch", "abc123", "--issue", "o/r#7"])
    assert rc == 0
    assert seen["batch_id"] == "abc123"
    assert seen["issue"] == "o/r#7"
    assert seen["apply"] is False
