import json

from triage_verse.cli import Output


def test_emit_json_envelope_success(capsys):
    rc = Output("sync", json_mode=True).emit({"issues": 4}, human="synced", exit_code=0)
    assert rc == 0
    out = capsys.readouterr()
    assert out.err == ""
    doc = json.loads(out.out)
    assert doc == {"command": "sync", "ok": True, "exit_code": 0, "data": {"issues": 4}}


def test_emit_human_prints_prose(capsys):
    rc = Output("sync", json_mode=False).emit({"issues": 4}, human="synced 4", exit_code=0)
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
