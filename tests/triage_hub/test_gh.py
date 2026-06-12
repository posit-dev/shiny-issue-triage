import json
import subprocess

import pytest

from triage_hub import gh


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_gh_returns_stdout(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeProc(stdout='{"ok": true}')

    monkeypatch.setattr(subprocess, "run", fake_run)

    out = gh.run_gh(["api", "rate_limit"])

    assert out == '{"ok": true}'
    assert calls == [["gh", "api", "rate_limit"]]


def test_run_gh_retries_on_rate_limit_then_raises(monkeypatch):
    sleeps = []
    attempts = []

    def fake_run(cmd, **kwargs):
        attempts.append(cmd)
        return FakeProc(returncode=1, stderr="HTTP 403: API rate limit exceeded")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(gh.GhError, match="rate limit"):
        gh.run_gh(["api", "x"], retries=3, sleep=sleeps.append)

    assert len(attempts) == 3
    assert sleeps == [30, 60]  # backoff doubles, no sleep after final attempt


def test_run_gh_fails_fast_on_other_errors(monkeypatch):
    attempts = []

    def fake_run(cmd, **kwargs):
        attempts.append(cmd)
        return FakeProc(returncode=1, stderr="HTTP 404: Not Found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(gh.GhError, match="404"):
        gh.run_gh(["api", "missing"], sleep=lambda s: None)

    assert len(attempts) == 1


def test_gh_json_parses(monkeypatch):
    monkeypatch.setattr(gh, "run_gh", lambda args, **kw: '[{"id": 1}]')
    assert gh.gh_json(["api", "things"]) == [{"id": 1}]


def test_gh_graphql_sends_payload_and_unwraps_data(monkeypatch):
    seen = {}

    def fake_run(args, *, input=None, **kw):
        seen["args"] = args
        seen["payload"] = json.loads(input)
        return json.dumps({"data": {"repository": {"name": "shiny"}}})

    monkeypatch.setattr(gh, "run_gh", fake_run)

    data = gh.gh_graphql("query($x: Int!) { n }", {"x": 1})

    assert data == {"repository": {"name": "shiny"}}
    assert seen["args"] == ["api", "graphql", "--input", "-"]
    assert seen["payload"] == {"query": "query($x: Int!) { n }", "variables": {"x": 1}}


def test_gh_graphql_raises_on_errors(monkeypatch):
    monkeypatch.setattr(
        gh, "run_gh",
        lambda args, **kw: json.dumps({"data": None,
                                       "errors": [{"message": "boom"}]}),
    )
    with pytest.raises(gh.GhError, match="boom"):
        gh.gh_graphql("query { n }", {})
