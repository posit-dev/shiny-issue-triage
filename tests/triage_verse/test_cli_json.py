import json

from triage_verse import analytics as analytics_mod
from triage_verse import analyze as analyze_mod
from triage_verse import cli
from triage_verse import executor as executor_mod
from triage_verse import snapshot as snapshot_mod
from triage_verse import sync as sync_mod
from triage_verse import tier2
from triage_verse import verify as verify_mod
from triage_verse.cli import Output


def test_emit_json_envelope_success(capsys):
    rc = Output("sync", json_mode=True).emit({"issues": 4}, human="synced", exit_code=0)
    assert rc == 0
    out = capsys.readouterr()
    assert out.err == ""
    doc = json.loads(out.out)
    assert doc == {"command": "sync", "ok": True, "exit_code": 0, "data": {"issues": 4}}


def test_emit_human_prints_prose(capsys):
    rc = Output("sync", json_mode=False).emit(
        {"issues": 4}, human="synced 4", exit_code=0
    )
    assert rc == 0
    out = capsys.readouterr()
    assert out.out.strip() == "synced 4"


def test_emit_preserves_nonzero_exit_code_with_ok_true(capsys):
    rc = Output("verify-counts", json_mode=True).emit(
        {"reconciled": False}, human="mismatch", exit_code=1
    )
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["exit_code"] == 1


def test_fail_json_envelope(capsys):
    rc = Output("sync", json_mode=True).fail("bad repo", exit_code=1)
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc == {"command": "sync", "ok": False, "exit_code": 1, "error": "bad repo"}


def test_fail_human_prints_to_stderr(capsys):
    rc = Output("sync", json_mode=False).fail("bad repo")
    assert rc == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == "error: bad repo"


def test_log_routes_to_stderr_in_json_mode(capsys):
    Output("sync", json_mode=True).log("progress")
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == "progress"


def test_log_routes_to_stdout_in_human_mode(capsys):
    Output("sync", json_mode=False).log("progress")
    out = capsys.readouterr()
    assert out.out.strip() == "progress"


def _repos_cfg(tmp_path):
    cfg = tmp_path / "repos.yaml"
    cfg.write_text("repositories:\n  - rstudio/shiny\n")
    return cfg


def test_sync_json_envelope(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    monkeypatch.setattr(
        sync_mod,
        "sync_all",
        lambda con, repos, *, full, log: {
            "repos": 1,
            "issues": 2,
            "prs": 0,
            "comments": 3,
        },
    )
    rc = cli.main(
        ["sync", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "sync"
    assert doc["ok"] is True
    assert doc["data"] == {"repos": 1, "issues": 2, "prs": 0, "comments": 3}


def test_json_flag_accepted_before_subcommand(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    monkeypatch.setattr(
        sync_mod,
        "sync_all",
        lambda con, repos, *, full, log: {
            "repos": 1,
            "issues": 0,
            "prs": 0,
            "comments": 0,
        },
    )
    rc = cli.main(
        ["--json", "sync", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["command"] == "sync"


def test_sync_logs_go_to_stderr_in_json_mode(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)

    def fake_sync_all(con, repos, *, full, log):
        log("mirroring rstudio/shiny")
        return {"repos": 1, "issues": 0, "prs": 0, "comments": 0}

    monkeypatch.setattr(sync_mod, "sync_all", fake_sync_all)
    cli.main(
        ["sync", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)]
    )
    out = capsys.readouterr()
    assert "mirroring" in out.err
    json.loads(out.out)  # stdout is exactly the envelope, still parseable


def test_sync_unknown_repo_json_error(tmp_path, capsys):
    cfg = _repos_cfg(tmp_path)
    rc = cli.main(
        [
            "sync",
            "--json",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--config",
            str(cfg),
            "--repo",
            "rstudio/nope",
        ]
    )
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and doc["exit_code"] == 1 and "nope" in doc["error"]


def test_unexpected_exception_becomes_json_envelope(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)

    def boom(con, repos, *, full, log):
        raise RuntimeError("network died")

    monkeypatch.setattr(sync_mod, "sync_all", boom)
    rc = cli.main(
        ["sync", "--json", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)]
    )
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc == {
        "command": "sync",
        "ok": False,
        "exit_code": 1,
        "error": "network died",
    }


def test_unexpected_exception_reraises_in_human_mode(tmp_path, monkeypatch):
    import pytest

    cfg = _repos_cfg(tmp_path)

    def boom(con, repos, *, full, log):
        raise RuntimeError("network died")

    monkeypatch.setattr(sync_mod, "sync_all", boom)
    with pytest.raises(RuntimeError, match="network died"):
        cli.main(["sync", "--db", str(tmp_path / "m.sqlite"), "--config", str(cfg)])


def test_verify_counts_mismatch_is_ok_true_exit_1(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    monkeypatch.setattr(
        verify_mod,
        "verify_counts",
        lambda con, repos, *, tolerance: [
            {"repo": "rstudio/shiny", "mirror": 10, "github": 12, "ok": False}
        ],
    )
    rc = cli.main(
        [
            "verify-counts",
            "--json",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--config",
            str(cfg),
        ]
    )
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True
    assert doc["exit_code"] == 1
    assert doc["data"]["reconciled"] is False
    assert doc["data"]["repos"][0]["diff"] == 2


def test_verify_counts_all_ok_exit_0(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    monkeypatch.setattr(
        verify_mod,
        "verify_counts",
        lambda con, repos, *, tolerance: [
            {"repo": "rstudio/shiny", "mirror": 10, "github": 10, "ok": True}
        ],
    )
    rc = cli.main(
        [
            "verify-counts",
            "--json",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--config",
            str(cfg),
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["data"]["reconciled"] is True


def test_analyze_status_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        analyze_mod,
        "analyze_status",
        lambda con: {"open_batches": [], "today_spend_usd": 1.25},
    )
    rc = cli.main(["analyze-status", "--json", "--db", str(tmp_path / "m.sqlite")])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["data"] == {"open_batches": [], "today_spend_usd": 1.25}


def test_tier2_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tier2, "request_fix", lambda repo, number, *, run_gh: None)
    rc = cli.main(["tier2", "--json", "rstudio/shiny#7"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["data"]["repo"] == "rstudio/shiny"
    assert doc["data"]["number"] == 7
    assert doc["data"]["label"] == tier2.LABEL


def test_tier2_bad_ref_json_error(capsys):
    rc = cli.main(["tier2", "--json", "not-a-ref"])
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and "not-a-ref" in doc["error"]


def test_execute_json_error_count_is_ok_true_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        executor_mod,
        "execute",
        lambda con, **kw: {"batch_id": "b1", "counts": {"applied": 3, "error": 1}},
    )
    rc = cli.main(["execute", "--json", "--db", str(tmp_path / "m.sqlite")])
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True
    assert doc["exit_code"] == 1
    assert doc["data"] == {"batch_id": "b1", "counts": {"applied": 3, "error": 1}}


def test_analytics_export_json_emits_payload(tmp_path, monkeypatch, capsys):
    payload = {"generated_at": "2026-07-15T00:00:00Z", "totals": {}, "repos": {}}
    monkeypatch.setattr(analytics_mod, "export", lambda con, out_path: payload)
    rc = cli.main(
        [
            "analytics",
            "export",
            "--json",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--out",
            str(tmp_path / "a.json"),
        ]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "analytics export"
    assert doc["data"] == payload


def test_snapshot_publish_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        snapshot_mod, "publish", lambda db, *, dated: snapshot_mod.LATEST_TAG
    )
    rc = cli.main(["snapshot", "publish", "--json", "--db", str(tmp_path / "m.sqlite")])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "snapshot publish"
    assert doc["data"] == {
        "tag": snapshot_mod.LATEST_TAG,
        "latest_tag": snapshot_mod.LATEST_TAG,
    }


def test_steady_state_dry_run_json(tmp_path, monkeypatch, capsys):
    cfg = _repos_cfg(tmp_path)
    rc = cli.main(
        [
            "steady-state",
            "--json",
            "--dry-run",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--config",
            str(cfg),
        ]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "steady-state"
    assert doc["data"]["dry_run"] is True
    assert "state-pull" in doc["data"]["stages"]


def test_steady_state_json_logs_routed_to_stderr(tmp_path, monkeypatch, capsys):
    """Regression: stage closures must use out.log, not print, so --json stdout stays pure."""
    from triage_verse import state

    cfg = _repos_cfg(tmp_path)

    # Patch _ensure_state_clone to no-op
    monkeypatch.setattr(cli, "_ensure_state_clone", lambda work_dir, branch: None)

    # Patch state.pull / state.push to no-op
    monkeypatch.setattr(state, "pull", lambda **kw: {})
    monkeypatch.setattr(state, "push", lambda *a, **kw: {})

    # Patch sync_all to log something, proving logs go to stderr not stdout
    def fake_sync(con, repos, *, full, log):
        log("mirroring progress")
        return {"repos": 1, "issues": 0, "prs": 0, "comments": 0}

    monkeypatch.setattr(sync_mod, "sync_all", fake_sync)

    # Patch _run_analyze to no-op (avoids needing embedder/model config)
    monkeypatch.setattr(cli, "_run_analyze", lambda args: {})

    # Patch snapshot publish to no-op
    monkeypatch.setattr(snapshot_mod, "publish", lambda db, *, dated: None)

    # Use --no-tier1 to skip tier1 entirely
    rc = cli.main(
        [
            "steady-state",
            "--json",
            "--no-tier1",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--config",
            str(cfg),
        ]
    )

    captured = capsys.readouterr()
    # stdout must be exactly one valid JSON envelope
    doc = json.loads(captured.out)
    assert doc["command"] == "steady-state"
    assert doc["ok"] is True
    assert rc == 0
    # The logged message must appear in stderr, not stdout
    assert "mirroring progress" in captured.err
    assert "mirroring progress" not in captured.out


def test_autonomy_status_json_empty(tmp_path, monkeypatch, capsys):
    from triage_verse import autonomy, review_queue

    monkeypatch.setattr(review_queue, "iter_jsonl_records", lambda d: [])
    monkeypatch.setattr(autonomy, "evaluate", lambda decisions, results, cfg: {})
    rc = cli.main(
        [
            "autonomy",
            "status",
            "--json",
            "--decisions-dir",
            str(tmp_path),
            "--results-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "autonomy status"
    assert doc["data"] == {"categories": {}, "wrote": None}


def test_proposals_prune_json_envelope(tmp_path, capsys):
    f = tmp_path / "proposals" / "2026" / "W27.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps(
            {"id": "bad-hyphen", "repo": "r/r", "issue": 1, "action": "add-label"}
        )
        + "\n",
        encoding="utf-8",
    )
    rc = cli.main(
        [
            "proposals",
            "prune",
            "--json",
            "bad-hyphen",
            "--proposals-dir",
            str(tmp_path / "proposals"),
        ]
    )
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "proposals prune"
    assert doc["ok"] is True
    assert doc["data"]["removed"] == 1
    assert doc["data"]["matches"][0]["id"] == "bad-hyphen"


def test_proposals_prune_valid_id_json_error(tmp_path, capsys):
    f = tmp_path / "proposals" / "2026" / "W27.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps({"id": "keepme", "repo": "r/r", "issue": 2, "action": "add-label"})
        + "\n",
        encoding="utf-8",
    )
    rc = cli.main(
        [
            "proposals",
            "prune",
            "--json",
            "keepme",
            "--proposals-dir",
            str(tmp_path / "proposals"),
        ]
    )
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False
    assert "valid module id" in doc["error"]
