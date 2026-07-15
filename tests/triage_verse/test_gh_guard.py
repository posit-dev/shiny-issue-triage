import json

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
