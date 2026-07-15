import json
import json as _json
import subprocess

import pytest

from triage_verse import gh

ALLOWED = frozenset({"o/r", "posit-dev/py-shiny"})


def _resolve():
    return ALLOWED


def _classify(args, **kw):
    kw.setdefault("resolve_allowed", _resolve)
    return gh.classify_gh_call(args, **kw)


# --- reads are allowed ---------------------------------------------------
def test_rest_get_read_allowed():
    _classify(["api", "repos/o/r/issues/5"])  # no raise


def test_graphql_query_read_allowed():
    payload = '{"query": "query($x: Int!) { n }", "variables": {}}'
    _classify(["api", "graphql", "--input", "-"], input=payload)


def test_repo_view_read_allowed():
    _classify(["repo", "view", "--json", "url"])


def test_release_view_and_list_allowed():
    _classify(["release", "view", "mirror-latest"])
    _classify(["release", "list", "--limit", "100", "--json", "tagName"])


# --- trusted infra: release writes allowed, redirection refused ----------
def test_release_create_upload_delete_allowed():
    _classify(["release", "create", "mirror-latest", "--title", "t"])
    _classify(["release", "upload", "mirror-latest", "f.zst", "--clobber"])
    _classify(["release", "delete", "mirror-2026-07-01", "--yes", "--cleanup-tag"])


def test_release_with_explicit_repo_refused():
    with pytest.raises(gh.EgressRefused, match="release"):
        _classify(["release", "create", "t", "--repo", "evil/repo"])


# --- non-graphql REST writes refused -------------------------------------
def test_rest_post_refused():
    with pytest.raises(gh.EgressRefused):
        _classify(["api", "-X", "POST", "repos/o/r/issues/5/comments", "-f", "body=hi"])


def test_rest_delete_refused():
    with pytest.raises(gh.EgressRefused):
        _classify(["api", "-X", "DELETE", "repos/o/r/issues/comments/1"])


def test_rest_body_flag_implies_write_refused():
    with pytest.raises(gh.EgressRefused):
        _classify(["api", "repos/o/r/issues/5/comments", "-f", "body=hi"])


# --- porcelain writes refused (the bypass guard) -------------------------
def test_porcelain_issue_edit_refused():
    with pytest.raises(gh.EgressRefused):
        _classify(["issue", "edit", "5", "--repo", "o/r", "--add-label", "bug"])


def test_porcelain_issue_close_refused():
    with pytest.raises(gh.EgressRefused):
        _classify(["issue", "close", "5", "--repo", "o/r", "--reason", "completed"])


def test_unknown_shape_refused():
    with pytest.raises(gh.EgressRefused):
        _classify(["auth", "token"])


# --- guarded graphql mutations -------------------------------------------
def _mutation_payload(field):
    query = "mutation($id: ID!) { %s(input: {issueId: $id}) { issue { id } } }" % field
    return json.dumps({"query": query, "variables": {}})


def test_mutation_allowed_when_operation_field_and_repo_ok():
    _classify(
        ["api", "graphql", "--input", "-"],
        input=_mutation_payload("closeIssue"),
        operation="closeIssue",
        repos=["o/r"],
    )


def test_mutation_refused_when_operation_not_allowlisted():
    with pytest.raises(gh.EgressRefused, match="operation"):
        _classify(
            ["api", "graphql", "--input", "-"],
            input=_mutation_payload("closeIssue"),
            operation="deleteRepository",
            repos=["o/r"],
        )


def test_mutation_refused_when_wire_field_not_allowlisted():
    with pytest.raises(gh.EgressRefused, match="field"):
        _classify(
            ["api", "graphql", "--input", "-"],
            input=_mutation_payload("deleteRepository"),
            operation="closeIssue",
            repos=["o/r"],
        )


def test_mutation_refused_when_repo_not_declared():
    with pytest.raises(gh.EgressRefused, match="repo"):
        _classify(
            ["api", "graphql", "--input", "-"],
            input=_mutation_payload("closeIssue"),
            operation="closeIssue",
            repos=[],
        )


def test_mutation_refused_when_repo_not_in_allowlist():
    with pytest.raises(gh.EgressRefused, match="allowlist"):
        _classify(
            ["api", "graphql", "--input", "-"],
            input=_mutation_payload("closeIssue"),
            operation="closeIssue",
            repos=["evil/repo"],
        )


def test_graphql_unparseable_payload_refused():
    with pytest.raises(gh.EgressRefused):
        _classify(["api", "graphql", "--input", "-"], input="{not json")


def test_repo_check_canonicalizes_case_and_whitespace():
    _classify(
        ["api", "graphql", "--input", "-"],
        input=_mutation_payload("closeIssue"),
        operation="closeIssue",
        repos=["  O/R  "],
    )


# --- mutation-field parser -----------------------------------------------
def test_mutation_fields_none_for_query():
    assert gh._mutation_fields("query { viewer { login } }") is None


def test_mutation_fields_single():
    q = "mutation($id: ID!) { closeIssue(input: {issueId: $id}) { issue { id } } }"
    assert gh._mutation_fields(q) == ["closeIssue"]


def test_mutation_fields_ignores_nested_and_args():
    q = (
        "mutation($id: ID!, $l: [ID!]!) { addLabelsToLabelable(input: "
        "{labelableId: $id, labelIds: $l}) { clientMutationId } }"
    )
    assert gh._mutation_fields(q) == ["addLabelsToLabelable"]


def test_mutation_fields_catches_aliased_injection():
    q = "mutation { safe: addComment(input: {}) { x } evil: deleteRepository(input: {}) { y } }"
    fields = gh._mutation_fields(q)
    assert "addComment" in fields and "deleteRepository" in fields


# --- run_gh guard integration + gh_mutation --------------------------------


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_gh_refuses_porcelain_write_before_subprocess(monkeypatch):
    launched = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: launched.append(cmd))
    with pytest.raises(gh.EgressRefused):
        gh.run_gh(["issue", "edit", "5", "--repo", "o/r", "--add-label", "bug"])
    assert launched == []  # refused before ever launching gh


def test_run_gh_allows_read(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **k: _FakeProc(stdout='{"ok": true}')
    )
    assert gh.run_gh(["api", "repos/o/r/issues/5"]) == '{"ok": true}'


def test_gh_mutation_validates_and_dispatches(monkeypatch):
    seen = {}

    def fake_run(args, *, input=None, operation=None, repos=None, **kw):
        seen["args"], seen["operation"], seen["repos"] = args, operation, repos
        seen["payload"] = _json.loads(input)
        return _json.dumps({"data": {"closeIssue": {"issue": {"id": "N1"}}}})

    monkeypatch.setattr(gh, "run_gh", fake_run)
    data = gh.gh_mutation(
        "closeIssue",
        "mutation($id: ID!) { closeIssue(input: {issueId: $id}) { issue { id } } }",
        {"id": "N1"},
        repos=["o/r"],
    )
    assert data == {"closeIssue": {"issue": {"id": "N1"}}}
    assert seen["operation"] == "closeIssue"
    assert seen["repos"] == ["o/r"]
    assert seen["args"] == ["api", "graphql", "--input", "-"]


def test_gh_mutation_refuses_unknown_operation():
    with pytest.raises(gh.EgressRefused, match="operation"):
        gh.gh_mutation("deleteRepository", "mutation { x }", {}, repos=["o/r"])
