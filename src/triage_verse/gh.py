"""Thin wrapper around the `gh` CLI (auth and HTTP handled by gh)."""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable

from . import config as _config

RETRYABLE_MARKERS = (
    "rate limit",
    "HTTP 429",
    "HTTP 502",
    "HTTP 503",
    "HTTP 504",
    "timeout",
)


class GhError(RuntimeError):
    pass


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
    "release": frozenset({"view", "list", "download", "create", "upload", "delete"})
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
        except (json.JSONDecodeError, AttributeError) as exc:
            raise EgressRefused(
                "unparseable graphql payload — refusing fail-closed"
            ) from exc
    # No query flag and no --input body: an empty request cannot mutate
    # anything, so return "" (classified as a read by _mutation_fields).
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
            raise EgressRefused(
                f"graphql mutation operation not allowlisted: {operation!r}"
            )
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
    has_body = any(a in _BODY_FLAGS or a.startswith(_BODY_FLAG_PREFIXES) for a in rest)
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
        if any(a in ("--repo", "-R") or a.startswith("--repo=") for a in args):
            raise EgressRefused(
                f"trusted-infra {cmd} op refused explicit --repo: {args[:2]}"
            )
        return
    reads = _READ_ONLY_PORCELAIN.get(cmd)
    if reads is not None and sub in reads:
        return
    raise EgressRefused(f"refused non-allowlisted gh call: {args[:2]}")


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
    for attempt in range(retries):
        try:
            proc = subprocess.run(
                ["gh", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                input=input,
            )
        except FileNotFoundError:
            raise GhError(
                "gh binary not found — install the GitHub CLI (https://cli.github.com)"
            ) from None
        if proc.returncode == 0:
            return proc.stdout
        last_error = proc.stderr.strip() or f"gh exited {proc.returncode}"
        retryable = any(
            marker.lower() in last_error.lower() for marker in RETRYABLE_MARKERS
        )
        if not retryable:
            raise GhError(last_error)
        if attempt < retries - 1:
            sleep(delay)
            delay *= 2
    raise GhError(last_error)


# Shared label-mutation vocabulary — used here (add_issue_label) and by the
# executor. Issues and PRs share these GraphQL fields (see ALLOWED_MUTATION_FIELDS).
ADD_LABELS_MUTATION = (
    "mutation($id: ID!, $labels: [ID!]!) { addLabelsToLabelable("
    "input: {labelableId: $id, labelIds: $labels}) { clientMutationId } }"
)
REMOVE_LABELS_MUTATION = (
    "mutation($id: ID!, $labels: [ID!]!) { removeLabelsFromLabelable("
    "input: {labelableId: $id, labelIds: $labels}) { clientMutationId } }"
)


def label_node_id(repo: str, name: str, *, run_gh: "Callable[..., str]") -> str:
    """Resolve a label's GraphQL node ID by name (a REST read, passes the guard)."""
    from urllib.parse import quote

    out = run_gh(["api", f"repos/{repo}/labels/{quote(name, safe='')}"])
    return json.loads(out)["node_id"]


def add_issue_label(
    repo: str, number: int, label: str, *, run_gh: "Callable[..., str] | None" = None
) -> None:
    """Add one label to an issue via the guarded GraphQL write path."""
    _run = run_gh if run_gh is not None else globals()["run_gh"]
    node_id = json.loads(_run(["api", f"repos/{repo}/issues/{number}"]))["node_id"]
    label_id = label_node_id(repo, label, run_gh=_run)
    gh_mutation(
        "addLabelsToIssue",
        ADD_LABELS_MUTATION,
        {"id": node_id, "labels": [label_id]},
        repos=[repo],
    )


def gh_json(args: list[str], **kwargs: Any) -> Any:
    out = run_gh(args, **kwargs)
    return json.loads(out) if out.strip() else None


def gh_graphql(query: str, variables: dict, **kwargs: Any) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    out = run_gh(["api", "graphql", "--input", "-"], input=payload, **kwargs)
    body = json.loads(out)
    # gh exits 1 for most GraphQL errors (including RATE_LIMITED), so run_gh
    # handles retry. This guard catches the rare HTTP-200-with-errors case
    # (partial success), which is not retried.
    if body.get("errors"):
        raise GhError(json.dumps(body["errors"]))
    return body["data"]


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
