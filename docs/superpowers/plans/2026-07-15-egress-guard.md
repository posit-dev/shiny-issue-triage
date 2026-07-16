# Credential Egress Guard for GitHub Writes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every credentialed GitHub *write* through a single fail-closed guard in `gh.py`, so a misfired prompt/bug/compromised dep cannot use the token against the wrong repo, endpoint, or verb.

**Architecture:** Unify all issue-mutating writes onto one transport (GraphQL) dispatched through a new `gh.gh_mutation()` helper. `gh.run_gh()` classifies every call before launching `gh`: safe reads and a bounded trusted-infra `release` category are allowed; GraphQL mutations must pass an operation-name allowlist, a wire-field allowlist parsed from the query, and a declared-repo check against `config/repos.yaml`; every other shape is refused. The executor, tier2, and reprex are migrated off porcelain/REST writes onto `gh_mutation`.

**Tech Stack:** Python 3, `gh` CLI, GitHub GraphQL API, pytest, ruff, pyright, uv.

## Global Constraints

- **Fail closed.** The classifier *allows* only positively-recognized shapes; anything else raises `EgressRefused`. Never widen the default to "allow unless known-bad."
- **`EgressRefused` subclasses the existing `gh.GhError`**, so current `except GhError` handling (e.g. `executor._fetch_issue`) still catches it, and it is *not* retried (not in `RETRYABLE_MARKERS`).
- **Canonicalize before matching.** Compare repos as trimmed, case-folded `owner/name` on both sides (the kata pagination lesson).
- **No new third-party dependencies.** Use stdlib (`re`, `json`) + existing `config.load_repos`.
- **Logging-verbosity convention.** Every refusal logs one clear line naming the refused operation/repo; `gh_mutation` logs operation + target repos before dispatch. Keep stdout line-buffered behavior unchanged.
- **Gate:** `make py-check` (ruff format check + lint, pyright, pytest) must pass at the end of every task that touches Python.
- **Operation names vs GraphQL fields.** Our *operation* names are the policy unit (`addLabelsToIssue`, `removeLabelsFromIssue`, `addComment`, `closeIssue`, `reopenIssue`, `deleteIssueComment`; `addLabelsToPR`/`removeLabelsFromPR` reserved, not wired). The *wire fields* are GitHub's real GraphQL field names (`addLabelsToLabelable`, `removeLabelsFromLabelable`, `addComment`, `closeIssue`, `reopenIssue`, `deleteIssueComment`). Labels: `addLabelsToIssue` compiles to `addLabelsToLabelable`.

---

## File Structure

- **`src/triage_verse/gh.py`** (modify) — owns the guard: `EgressRefused`, the two allowlists + trusted-infra table, `_canon`, `_active_repos` (cached repo allowlist), `_graphql_query`, `_mutation_fields`, `classify_gh_call`, the `run_gh` wiring (new `operation`/`repos` kwargs), and the `gh_mutation` helper. Trust boundary; depends on `config.load_repos`.
- **`src/triage_verse/executor.py`** (modify) — `_apply_mutation`/`_apply_reverse` rewritten to build GraphQL mutations dispatched via `gh.gh_mutation`; add `_label_node_id` and `_comment_node_id` REST-read helpers.
- **`src/triage_verse/tier2.py`** (modify) — `request_fix` adds a label via `gh_mutation` (operation `addLabelsToIssue`).
- **`src/triage_verse/reprex.py`** (modify) — `request_reprex` likewise.
- **`tests/triage_verse/test_gh_guard.py`** (create) — unit tests for the classifier, parser, repo check, and `gh_mutation`.
- **`tests/triage_verse/fake_gh.py`** (modify) — interpret all-GraphQL mutations + the two node-ID resolution reads.
- **`tests/triage_verse/test_executor_execute.py`, `test_executor_undo.py`** (modify) — updated for GraphQL command shapes.
- **`tests/triage_verse/test_tier2.py`, `test_reprex.py`** (modify) — updated for the `gh_mutation` call.
- **`CLAUDE.md`** (modify) — architecture note: `gh.py` is the credentialed-write choke point.

---

## Task 1: Guard core in `gh.py` (pure classifier + parser)

Build the fail-closed classifier and its helpers as pure functions. No `run_gh` wiring yet — that is Task 2.

**Files:**
- Modify: `src/triage_verse/gh.py`
- Test: `tests/triage_verse/test_gh_guard.py`

**Interfaces:**
- Consumes: `config.load_repos(path) -> list[Repo]` (each `Repo` has `.full` → `"owner/name"`).
- Produces:
  - `class EgressRefused(GhError)`
  - `ALLOWED_OPERATIONS: frozenset[str]`, `ALLOWED_MUTATION_FIELDS: frozenset[str]`
  - `_canon(repo: str) -> str`
  - `_graphql_query(rest: list[str], input: str | None) -> str`
  - `_mutation_fields(query: str) -> list[str] | None` — `None` if the query is not a mutation (a read), else the list of top-level mutation field names.
  - `classify_gh_call(args: list[str], *, input: str | None = None, operation: str | None = None, repos: list[str] | None = None, resolve_allowed: Callable[[], frozenset[str]]) -> None` — returns `None` if allowed, raises `EgressRefused` otherwise. `resolve_allowed` is called *only* on the mutation branch (no config IO for reads).

- [ ] **Step 1: Write the failing tests**

Create `tests/triage_verse/test_gh_guard.py`:

```python
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
    query = (
        "mutation($id: ID!) { %s(input: {issueId: $id}) { issue { id } } }" % field
    )
    return '{"query": %r, "variables": {}}' % query


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_gh_guard.py -q`
Expected: FAIL — `AttributeError: module 'triage_verse.gh' has no attribute 'EgressRefused'` (and friends).

- [ ] **Step 3: Implement the guard core in `gh.py`**

Add these imports near the top of `src/triage_verse/gh.py` (it currently imports `json`, `subprocess`, `time`, and `typing`):

```python
import re

from . import config as _config
```

Add, after the existing `class GhError(RuntimeError): pass`:

```python
class EgressRefused(GhError):
    """A GitHub call was refused by the egress guard before leaving the process."""


REPOS_PATH = "config/repos.yaml"

# Our operation names — the policy unit the guard checks (issue/PR granular).
# addLabelsToPR / removeLabelsFromPR are reserved for when the apply stage
# touches PRs; nothing labels PRs today, so they are intentionally absent.
ALLOWED_OPERATIONS = frozenset(
    {
        "addLabelsToIssue",
        "removeLabelsFromIssue",
        "addComment",
        "closeIssue",
        "reopenIssue",
        "deleteIssueComment",
    }
)

# GitHub's real GraphQL mutation field names — the fail-closed wire backstop.
# addLabelsToIssue/addLabelsToPR both compile to addLabelsToLabelable.
ALLOWED_MUTATION_FIELDS = frozenset(
    {
        "addLabelsToLabelable",
        "removeLabelsFromLabelable",
        "addComment",
        "closeIssue",
        "reopenIssue",
        "deleteIssueComment",
    }
)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_BODY_FLAG_PREFIXES = ("--field=", "--raw-field=", "--input=")
_BODY_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
# Read-only porcelain the codebase actually uses.
_READ_ONLY_PORCELAIN = {"repo": frozenset({"view"})}
# Trusted-infra writes: snapshot's state-bus releases on the hub repo. Bounded
# to the ambient checkout by refusing any explicit --repo redirection.
_TRUSTED_INFRA = {
    "release": frozenset({"view", "list", "create", "upload", "delete"})
}


def _canon(repo: str) -> str:
    return repo.strip().casefold()


_active_repos_cache: frozenset[str] | None = None


def _active_repos() -> frozenset[str]:
    global _active_repos_cache
    if _active_repos_cache is None:
        _active_repos_cache = frozenset(
            _canon(r.full) for r in _config.load_repos(REPOS_PATH)
        )
    return _active_repos_cache


def _graphql_query(rest: list[str], input: str | None) -> str:
    """Extract the GraphQL query text from `-f query=...` args or --input JSON."""
    for a in rest:
        if a.startswith("query="):
            return a[len("query=") :]
    if input:
        try:
            return str(json.loads(input).get("query", ""))
        except (json.JSONDecodeError, AttributeError):
            return ""
    return ""


def _mutation_fields(query: str) -> list[str] | None:
    """Top-level mutation field names, or None if `query` is not a mutation.

    We construct these queries ourselves, so field names are reliably present as
    literal text. A top-level field is an identifier at brace-depth 1 that is
    immediately followed by `(` (arguments) or `{` (subselection); alias keys
    (`name:`) and argument names are skipped, so the real field an alias points
    at is still counted.
    """
    s = query.strip()
    if not re.match(r"mutation\b", s):
        return None
    start = s.find("{")
    if start == -1:
        return []
    fields: list[str] = []
    depth = 0
    i = start
    n = len(s)
    while i < n:
        c = s[i]
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue
        if depth == 1 and (c.isalpha() or c == "_"):
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            k = j
            while k < n and s[k].isspace():
                k += 1
            if k < n and s[k] in "({":
                fields.append(s[i:j])
            i = j
            continue
        i += 1
    return fields


def _classify_api(
    rest: list[str],
    *,
    input: str | None,
    operation: str | None,
    repos: list[str] | None,
    resolve_allowed: "Callable[[], frozenset[str]]",
) -> None:
    if rest and rest[0] == "graphql":
        fields = _mutation_fields(_graphql_query(rest, input))
        if fields is None:
            return  # graphql read
        if operation not in ALLOWED_OPERATIONS:
            raise EgressRefused(f"graphql mutation operation not allowlisted: {operation!r}")
        for f in fields:
            if f not in ALLOWED_MUTATION_FIELDS:
                raise EgressRefused(f"graphql mutation field not allowlisted: {f!r}")
        if not repos:
            raise EgressRefused("graphql mutation without declared target repos")
        allowed = resolve_allowed()
        for r in repos:
            if _canon(r) not in allowed:
                raise EgressRefused(f"repo not in allowlist: {r!r}")
        return
    method = None
    for idx, a in enumerate(rest):
        if a in ("-X", "--method") and idx + 1 < len(rest):
            method = rest[idx + 1].upper()
        elif a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
    has_body = any(
        a in _BODY_FLAGS or a.startswith(_BODY_FLAG_PREFIXES) for a in rest
    )
    if method in _MUTATING_METHODS or has_body:
        raise EgressRefused(f"non-graphql REST write refused: {rest[:2]}")
    return  # REST GET read


def classify_gh_call(
    args: list[str],
    *,
    input: str | None = None,
    operation: str | None = None,
    repos: list[str] | None = None,
    resolve_allowed: "Callable[[], frozenset[str]]" = _active_repos,
) -> None:
    """Raise EgressRefused unless `args` is a positively-recognized safe call."""
    if not args:
        raise EgressRefused("empty gh invocation")
    cmd, sub = args[0], (args[1] if len(args) > 1 else "")
    if cmd == "api":
        _classify_api(
            args[1:],
            input=input,
            operation=operation,
            repos=repos,
            resolve_allowed=resolve_allowed,
        )
        return
    infra = _TRUSTED_INFRA.get(cmd)
    if infra is not None and sub in infra:
        if any(
            a in ("--repo", "-R") or a.startswith("--repo=") for a in args
        ):
            raise EgressRefused(f"trusted-infra {cmd} op refused explicit --repo: {args[:2]}")
        return
    reads = _READ_ONLY_PORCELAIN.get(cmd)
    if reads is not None and sub in reads:
        return
    raise EgressRefused(f"refused non-allowlisted gh call: {args[:2]}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_gh_guard.py -q`
Expected: PASS (all cases).

- [ ] **Step 5: Type/lint check and commit**

Run: `make py-check`
Expected: PASS.

```bash
git add src/triage_verse/gh.py tests/triage_verse/test_gh_guard.py
git commit -m "feat(gh): fail-closed egress classifier + mutation parser (#41)"
```

---

## Task 2: Wire the guard into `run_gh` + add `gh_mutation`

Make the guard unbypassable (every `run_gh` call is classified) and provide the single write helper.

**Files:**
- Modify: `src/triage_verse/gh.py`
- Test: `tests/triage_verse/test_gh_guard.py`

**Interfaces:**
- Consumes: `classify_gh_call`, `EgressRefused`, `ALLOWED_OPERATIONS` (Task 1).
- Produces:
  - `run_gh(args, *, input=None, retries=5, sleep=time.sleep, operation=None, repos=None) -> str` — now guards before launching.
  - `gh_mutation(operation: str, query: str, variables: dict, *, repos: list[str], **kwargs) -> dict` — validates operation, builds the `{query, variables}` payload, calls `run_gh(["api","graphql","--input","-"], input=payload, operation=operation, repos=repos)`, raises `GhError` on GraphQL `errors`, returns `body["data"]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/triage_verse/test_gh_guard.py`)

```python
import json as _json
import subprocess


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_gh_guard.py -q`
Expected: FAIL — `run_gh` has no `operation`/`repos` kwargs / launches anyway; `gh_mutation` missing.

- [ ] **Step 3: Wire the guard and add `gh_mutation`**

In `src/triage_verse/gh.py`, change the `run_gh` signature and add the guard call at the very top of the body (before the retry loop):

```python
def run_gh(
    args: list[str],
    *,
    input: str | None = None,
    retries: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    operation: str | None = None,
    repos: list[str] | None = None,
) -> str:
    classify_gh_call(args, input=input, operation=operation, repos=repos)
    delay = 30.0
    last_error = "gh failed"
    # ... existing retry loop unchanged ...
```

Add `gh_mutation` (near `gh_graphql`):

```python
def gh_mutation(
    operation: str,
    query: str,
    variables: dict,
    *,
    repos: list[str],
    **kwargs: Any,
) -> dict:
    """Dispatch a guarded GraphQL mutation. Single write path for issue writes."""
    if operation not in ALLOWED_OPERATIONS:
        raise EgressRefused(f"operation not allowlisted: {operation!r}")
    payload = json.dumps({"query": query, "variables": variables})
    out = run_gh(
        ["api", "graphql", "--input", "-"],
        input=payload,
        operation=operation,
        repos=repos,
        **kwargs,
    )
    body = json.loads(out)
    if body.get("errors"):
        raise GhError(json.dumps(body["errors"]))
    return body["data"]
```

- [ ] **Step 4: Run the full gh + guard tests**

Run: `uv run pytest tests/triage_verse/test_gh_guard.py tests/triage_verse/test_gh.py -q`
Expected: PASS. (The existing `test_gh.py` reads — `api rate_limit`, `api x`, `api missing` — classify as REST GET reads and still pass. `gh_graphql`'s tests monkeypatch `run_gh`, so they are unaffected.)

- [ ] **Step 5: Type/lint check and commit**

Run: `make py-check`
Expected: PASS.

```bash
git add src/triage_verse/gh.py tests/triage_verse/test_gh_guard.py
git commit -m "feat(gh): guard run_gh + gh_mutation single write path (#41)"
```

---

## Task 3: Migrate the executor to guarded GraphQL

Rewrite `_apply_mutation`/`_apply_reverse` to build GraphQL mutations dispatched via `gh_mutation`, add node-ID resolution reads, and update the `FakeGh` test double + executor tests.

**Files:**
- Modify: `src/triage_verse/executor.py`
- Modify: `tests/triage_verse/fake_gh.py`
- Modify: `tests/triage_verse/test_executor_execute.py`, `tests/triage_verse/test_executor_undo.py`
- (Run, may need touch-ups: `test_executor_auto.py`, `test_executor_plan.py`, `test_executor_select.py`)

**Interfaces:**
- Consumes: `gh.gh_mutation(operation, query, variables, *, repos)`; `run_gh` for REST reads (`repos/{repo}/labels/{name}`, `repos/{repo}/issues/comments/{id}`).
- Produces (internal to executor): `_label_node_id(run_gh, repo, name) -> str`, `_comment_node_id(run_gh, repo, comment_id) -> str`. `_apply_mutation` keeps its signature `(run_gh, repo, number, node_id, mutation) -> int | None` (returns the created comment's `databaseId` for `comment` mutations, `None` otherwise), so result records still carry `comment_id`.

- [ ] **Step 1: Rewrite the write helpers in `executor.py`**

Add `from . import gh as gh_mod` to the imports. Replace `_apply_mutation` (the porcelain/REST version) with GraphQL builders:

```python
_CLOSE_STATE_REASON = {"completed": "COMPLETED", "not planned": "NOT_PLANNED"}

_ADD_LABELS = (
    "mutation($id: ID!, $labels: [ID!]!) { addLabelsToLabelable("
    "input: {labelableId: $id, labelIds: $labels}) { clientMutationId } }"
)
_REMOVE_LABELS = (
    "mutation($id: ID!, $labels: [ID!]!) { removeLabelsFromLabelable("
    "input: {labelableId: $id, labelIds: $labels}) { clientMutationId } }"
)
_ADD_COMMENT = (
    "mutation($id: ID!, $body: String!) { addComment(input: {subjectId: $id,"
    " body: $body}) { commentEdge { node { databaseId } } } }"
)
_CLOSE_ISSUE = (
    "mutation($id: ID!, $reason: IssueClosedStateReason) { closeIssue("
    "input: {issueId: $id, stateReason: $reason}) { issue { id } } }"
)
_CLOSE_DUPLICATE = (
    "mutation($id: ID!, $dup: ID!) { closeIssue(input: {issueId: $id,"
    " stateReason: DUPLICATE, duplicateIssueId: $dup}) { issue { id } } }"
)
_REOPEN_ISSUE = "mutation($id: ID!) { reopenIssue(input: {issueId: $id}) { issue { id } } }"
_DELETE_COMMENT = (
    "mutation($id: ID!) { deleteIssueComment(input: {id: $id}) { clientMutationId } }"
)


def _label_node_id(run_gh: RunGh, repo: str, name: str) -> str:
    from urllib.parse import quote

    out = run_gh(["api", f"repos/{repo}/labels/{quote(name, safe='')}"])
    return json.loads(out)["node_id"]


def _comment_node_id(run_gh: RunGh, repo: str, comment_id: int) -> str:
    out = run_gh(["api", f"repos/{repo}/issues/comments/{comment_id}"])
    return json.loads(out)["node_id"]


def _apply_mutation(
    run_gh: RunGh, repo: str, number: int, node_id: str, mutation: dict
) -> int | None:
    """Perform one mutation via guarded GraphQL; returns comment databaseId if any."""
    kind = mutation["kind"]
    if kind == "add-label":
        label_id = _label_node_id(run_gh, repo, mutation["label"])
        gh_mod.gh_mutation(
            "addLabelsToIssue",
            _ADD_LABELS,
            {"id": node_id, "labels": [label_id]},
            repos=[repo],
        )
    elif kind == "remove-label":
        label_id = _label_node_id(run_gh, repo, mutation["label"])
        gh_mod.gh_mutation(
            "removeLabelsFromIssue",
            _REMOVE_LABELS,
            {"id": node_id, "labels": [label_id]},
            repos=[repo],
        )
    elif kind == "comment":
        data = gh_mod.gh_mutation(
            "addComment",
            _ADD_COMMENT,
            {"id": node_id, "body": mutation["body"]},
            repos=[repo],
        )
        return data["addComment"]["commentEdge"]["node"]["databaseId"]
    elif kind == "close":
        gh_mod.gh_mutation(
            "closeIssue",
            _CLOSE_ISSUE,
            {"id": node_id, "reason": _CLOSE_STATE_REASON[mutation["reason"]]},
            repos=[repo],
        )
    elif kind == "close-duplicate":
        dup_repo, dup_number = mutation["canonical"]
        dup = _fetch_issue(run_gh, dup_repo, dup_number)
        gh_mod.gh_mutation(
            "closeIssue",
            _CLOSE_DUPLICATE,
            {"id": node_id, "dup": dup["node_id"]},
            repos=[repo],
        )
    return None
```

Note: `run_gh` is still threaded through for the REST *reads* (`_fetch_issue`, `_label_node_id`, `_comment_node_id`); only the *writes* switch to `gh_mod.gh_mutation`. The `node_id` parameter (the issue's GraphQL node ID, already fetched by `_fetch_issue`) is now used for every mutation.

- [ ] **Step 2: Rewrite `_apply_reverse` for the delete-comment + reopen mutations**

Replace `_apply_reverse` with:

```python
def _apply_reverse(run_gh: RunGh, repo: str, number: int, mutation: dict) -> None:
    kind = mutation["kind"]
    if kind in ("add-label", "remove-label"):
        node_id = json.loads(run_gh(["api", f"repos/{repo}/issues/{number}"]))["node_id"]
        _apply_mutation(run_gh, repo, number, node_id, mutation)
    elif kind == "delete-comment":
        comment_node = _comment_node_id(run_gh, repo, mutation["comment_id"])
        gh_mod.gh_mutation(
            "deleteIssueComment", _DELETE_COMMENT, {"id": comment_node}, repos=[repo]
        )
    elif kind == "reopen":
        node_id = json.loads(run_gh(["api", f"repos/{repo}/issues/{number}"]))["node_id"]
        gh_mod.gh_mutation(
            "reopenIssue", _REOPEN_ISSUE, {"id": node_id}, repos=[repo]
        )
```

(The old `_apply_reverse` called `_apply_mutation(..., "", mutation)` for labels with an empty node_id; labels now need the real issue node ID, hence the fetch. `delete-comment` and `reopen` become their own GraphQL mutations.)

- [ ] **Step 3: Rewrite `FakeGh` to interpret all-GraphQL mutations**

Replace `tests/triage_verse/fake_gh.py` entirely with:

```python
"""Stateful in-memory fake for gh.run_gh: all writes are GraphQL mutations."""

from __future__ import annotations

import json
import re


class FakeGh:
    """Callable standing in for gh.run_gh. Issues keyed by (repo, number)."""

    def __init__(self, issues: dict[tuple[str, int], dict]):
        # Each issue dict: labels (list[str]), state ("open"/"closed"),
        # state_reason (str|None), updated_at (str), node_id (str).
        self.issues = {k: dict(v) for k, v in issues.items()}
        self.comments: dict[int, dict] = {}  # databaseId -> {repo, number, body}
        self._next_comment_id = 1000
        self.mutating_calls: list[list[str]] = []

    def __call__(self, args: list[str], *, input=None, **kwargs) -> str:
        if args[0] != "api":
            raise AssertionError(f"unexpected gh args: {args}")
        if args[1] == "graphql":
            return self._graphql(input)
        return self._rest_read(args)

    # -- reads -----------------------------------------------------------

    def _rest_read(self, args: list[str]) -> str:
        path = args[1]
        m = re.match(r"repos/([\w.-]+/[\w.-]+)/labels/(.+)$", path)
        if m:  # label node-id resolution
            from urllib.parse import unquote

            return json.dumps({"node_id": f"L:{m.group(1)}:{unquote(m.group(2))}"})
        m = re.match(r"repos/([\w.-]+/[\w.-]+)/issues/comments/(\d+)$", path)
        if m:  # comment node-id resolution
            return json.dumps({"node_id": f"C:{m.group(2)}"})
        m = re.match(r"repos/([\w.-]+/[\w.-]+)/issues/(\d+)$", path)
        assert m, path
        issue = self.issues[(m.group(1), int(m.group(2)))]
        return json.dumps(
            {
                "updated_at": issue["updated_at"],
                "node_id": issue["node_id"],
                "state": issue["state"],
                "state_reason": issue["state_reason"],
                "labels": [{"name": name} for name in issue["labels"]],
            }
        )

    # -- graphql mutations ----------------------------------------------

    def _by_node_id(self, node_id: str) -> dict:
        for issue in self.issues.values():
            if issue["node_id"] == node_id:
                return issue
        raise AssertionError(f"unknown node id {node_id}")

    def _graphql(self, input: str) -> str:
        payload = json.loads(input)
        query, variables = payload["query"], payload["variables"]
        self.mutating_calls.append(["api", "graphql", query])
        if "addLabelsToLabelable" in query:
            issue = self._by_node_id(variables["id"])
            for lid in variables["labels"]:
                name = lid.split(":", 2)[2]
                if name not in issue["labels"]:
                    issue["labels"] = [*issue["labels"], name]
            return json.dumps({"data": {"addLabelsToLabelable": {"clientMutationId": None}}})
        if "removeLabelsFromLabelable" in query:
            issue = self._by_node_id(variables["id"])
            for lid in variables["labels"]:
                name = lid.split(":", 2)[2]
                issue["labels"] = [x for x in issue["labels"] if x != name]
            return json.dumps({"data": {"removeLabelsFromLabelable": {"clientMutationId": None}}})
        if "addComment" in query:
            cid = self._next_comment_id
            self._next_comment_id += 1
            issue = self._by_node_id(variables["id"])
            (repo, number), = [k for k, v in self.issues.items() if v is issue]
            self.comments[cid] = {"repo": repo, "number": number, "body": variables["body"]}
            return json.dumps(
                {"data": {"addComment": {"commentEdge": {"node": {"databaseId": cid}}}}}
            )
        if "deleteIssueComment" in query:
            cid = int(variables["id"].split(":", 1)[1])
            del self.comments[cid]
            return json.dumps({"data": {"deleteIssueComment": {"clientMutationId": None}}})
        if "reopenIssue" in query:
            issue = self._by_node_id(variables["id"])
            issue["state"], issue["state_reason"] = "open", "reopened"
            return json.dumps({"data": {"reopenIssue": {"issue": {"id": variables["id"]}}}})
        if "closeIssue" in query:
            issue = self._by_node_id(variables["id"])
            issue["state"] = "closed"
            if "DUPLICATE" in query:
                assert self._by_node_id(variables["dup"]) is not None
                issue["state_reason"] = "duplicate"
            else:
                issue["state_reason"] = (
                    "completed" if variables["reason"] == "COMPLETED" else "not_planned"
                )
            return json.dumps({"data": {"closeIssue": {"issue": {"id": variables["id"]}}}})
        raise AssertionError(f"unexpected graphql query: {query}")
```

- [ ] **Step 4: Update executor tests for GraphQL shapes**

The state-based assertions (`gh.issues[...]["labels"]`, `gh.comments`, `rec["status"]`, `rec["comment_id"] in gh.comments`, mirror rows) remain valid because `FakeGh` produces the same observable state. Two shape-specific spots need review:

In `test_executor_execute.py`, the close-duplicate test (`test_apply_close_duplicate_uses_graphql_duplicate_close`, ~line 125) already asserts duplicate-close behavior via state — confirm it still checks `issue["state"] == "closed"` and `state_reason == "duplicate"`; no change needed if so. The dry-run test's `assert gh.mutating_calls == []` remains correct (dry-run performs no mutations).

In `test_executor_undo.py`, `before = len(gh.mutating_calls)` / `assert len(gh.mutating_calls) == before` (idempotent re-undo, ~lines 109/119) remain valid — `mutating_calls` still only grows on GraphQL writes.

Run the executor suite and fix any assertion that inspects raw `gh` *argument shapes* (e.g. any leftover expectation of `["issue","edit",...]` or `-f body=`); rewrite such assertions to check resulting **state** (labels/comments/state) instead.

Run: `uv run pytest tests/triage_verse/test_executor_execute.py tests/triage_verse/test_executor_undo.py tests/triage_verse/test_executor_auto.py tests/triage_verse/test_executor_plan.py tests/triage_verse/test_executor_select.py -q`
Expected: PASS. Fix any failures per the guidance above until green.

- [ ] **Step 5: Type/lint check and commit**

Run: `make py-check`
Expected: PASS.

```bash
git add src/triage_verse/executor.py tests/triage_verse/fake_gh.py tests/triage_verse/test_executor_*.py
git commit -m "feat(executor): issue writes via guarded GraphQL mutations (#41)"
```

---

## Task 4: Migrate tier2 and reprex to `gh_mutation`

The two porcelain `issue edit --add-label` call sites must move to the guarded path (the classifier now refuses porcelain writes).

**Files:**
- Modify: `src/triage_verse/tier2.py`, `src/triage_verse/reprex.py`
- Test: `tests/triage_verse/test_tier2.py`, `tests/triage_verse/test_reprex.py`

**Interfaces:**
- Consumes: `executor._apply_mutation`-style add-label via GraphQL is *not* reused here (executor is a different layer); instead tier2/reprex call `gh.gh_mutation` directly after resolving the issue + label node IDs. To keep them decoupled from executor, add a tiny shared helper in `gh.py`:
  - Produces: `gh.add_issue_label(repo: str, number: int, label: str, *, run_gh=run_gh) -> None` — resolves the issue node ID (`repos/{repo}/issues/{number}`) and label node ID (`repos/{repo}/labels/{name}`) via REST reads, then dispatches `addLabelsToIssue`.

- [ ] **Step 1: Write the failing tests**

Replace `test_request_fix_adds_label_via_gh` in `tests/triage_verse/test_tier2.py` with a state-based test using `FakeGh`:

```python
import importlib.util
import pathlib as _pathlib

_spec = importlib.util.spec_from_file_location(
    "fake_gh", _pathlib.Path(__file__).parent / "fake_gh.py"
)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
FakeGh = _m.FakeGh


def test_request_fix_adds_label_via_graphql():
    gh = FakeGh(
        {("o/r", 7): {"labels": [], "state": "open", "state_reason": None,
                      "updated_at": "t", "node_id": "N7"}}
    )
    tier2.request_fix("o/r", 7, run_gh=gh)
    assert "ai-triage:fix-requested" in gh.issues[("o/r", 7)]["labels"]
    assert gh.mutating_calls  # a GraphQL mutation was dispatched
```

Add the analogous test to `tests/triage_verse/test_reprex.py` (label `ai-triage:needs-reprex`, node_id `"N7"`). Keep the existing `labels.yaml` assertions in `test_tier2.py` unchanged.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_tier2.py tests/triage_verse/test_reprex.py -q`
Expected: FAIL — `request_fix` still emits porcelain (which `FakeGh` no longer handles / would be refused).

- [ ] **Step 3: Add the shared helper and migrate both call sites**

In `src/triage_verse/gh.py`, add:

```python
def add_issue_label(
    repo: str, number: int, label: str, *, run_gh: "Callable[..., str]" = None
) -> None:
    """Add one label to an issue via the guarded GraphQL write path."""
    from urllib.parse import quote

    _run = run_gh if run_gh is not None else globals()["run_gh"]
    node_id = json.loads(_run(["api", f"repos/{repo}/issues/{number}"]))["node_id"]
    label_id = json.loads(
        _run(["api", f"repos/{repo}/labels/{quote(label, safe='')}"])
    )["node_id"]
    query = (
        "mutation($id: ID!, $labels: [ID!]!) { addLabelsToLabelable("
        "input: {labelableId: $id, labelIds: $labels}) { clientMutationId } }"
    )
    gh_mutation(
        "addLabelsToIssue", query, {"id": node_id, "labels": [label_id]}, repos=[repo]
    )
```

Update `src/triage_verse/tier2.py`:

```python
from __future__ import annotations

from typing import Callable

from . import gh as gh_mod

LABEL = "ai-triage:fix-requested"


def request_fix(
    repo: str, number: int, *, run_gh: Callable[..., str], label: str = LABEL
) -> None:
    gh_mod.add_issue_label(repo, number, label, run_gh=run_gh)
```

Update `src/triage_verse/reprex.py`'s `request_reprex` identically (keep its module docstring and `LABEL = "ai-triage:needs-reprex"`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_tier2.py tests/triage_verse/test_reprex.py tests/triage_verse/test_review_app_tier2.py tests/triage_verse/test_review_app_reprex.py -q`
Expected: PASS. (If the review-app tests stub `run_gh`, confirm they exercise `request_fix`/`request_reprex` through the new path; adjust any that asserted the old porcelain args to assert resulting label state.)

- [ ] **Step 5: Type/lint check and commit**

Run: `make py-check`
Expected: PASS.

```bash
git add src/triage_verse/gh.py src/triage_verse/tier2.py src/triage_verse/reprex.py tests/triage_verse/test_tier2.py tests/triage_verse/test_reprex.py
git commit -m "feat(tier2,reprex): label writes via guarded GraphQL (#41)"
```

---

## Task 5: Document the choke point + full-suite gate

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `CLAUDE.md`**

In the **Architecture** section, in the bullet describing `execute`/GitHub writes, add a sentence establishing the choke point. Append this paragraph after the `execute` bullet (adjust wording to match surrounding prose):

```markdown
- **egress guard** (`gh.py`): every credentialed GitHub call funnels through
  `gh.run_gh`, which **fails closed** — it allows recognized reads and a bounded
  trusted-infra `release` category, and refuses everything else. All
  issue-mutating **writes** go through `gh.gh_mutation` (a single GraphQL
  transport) and must pass an operation-name allowlist, a wire-field allowlist
  parsed from the query, and a declared-repo check against `config/repos.yaml`.
  Add new writes via `gh.gh_mutation`/`gh.add_issue_label`; porcelain and REST
  writes (`gh issue edit`, `gh api -X POST …`) are intentionally refused, so a
  new write call site fails closed rather than bypassing the guard.
```

- [ ] **Step 2: Run the full CI gate**

Run: `make check`
Expected: PASS (`validate-yaml`, `compile-scripts`, `py-check`, `js-check`).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document gh.py egress-guard choke point (#41)"
```

---

## Self-Review

**Spec coverage.**
- Transport unification (all-GraphQL, six operations, node-ID resolution) → Task 3 (executor) + Task 4 (tier2/reprex `add_issue_label`).
- Guard in `run_gh`, fail-closed classifier (read / mutation / trusted-infra / refuse) → Tasks 1–2.
- Operation-name allowlist + wire-field allowlist (two independent checks) → Task 1 (`classify_gh_call`) + Task 2 (`gh_mutation` operation check).
- Declared-repo check against `repos.yaml`, canonicalized → Task 1 (`_classify_api`, `_canon`, `_active_repos`).
- `EgressRefused` subclasses `GhError`, not retried → Task 1 (definition) + Global Constraints.
- Trusted-infra `release` category bounded to hub repo (no `--repo` redirection) → Task 1.
- Testing (classifier per-shape, parser, repo check, bypass guard, executor shapes) → Tasks 1–4.
- Logging on refusal / dispatch → messages in `EgressRefused`; `make py-check` gate each task.
- CLAUDE.md note → Task 5.

**Placeholder scan.** No TBD/TODO; every code step shows complete code; every test step shows real assertions.

**Type consistency.** `classify_gh_call` signature identical in Task 1 def and Task 2 call. `gh_mutation(operation, query, variables, *, repos)` identical in Tasks 2/3/4. `_apply_mutation(run_gh, repo, number, node_id, mutation) -> int | None` preserved from the original. `add_issue_label(repo, number, label, *, run_gh)` consistent across Task 4 def and both call sites. Comment identifiers: `databaseId` returned by `addComment` → stored as `comment_id` → resolved by `_comment_node_id` for `deleteIssueComment`.
