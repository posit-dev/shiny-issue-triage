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
            return json.dumps(
                {"data": {"addLabelsToLabelable": {"clientMutationId": None}}}
            )
        if "removeLabelsFromLabelable" in query:
            issue = self._by_node_id(variables["id"])
            for lid in variables["labels"]:
                name = lid.split(":", 2)[2]
                issue["labels"] = [x for x in issue["labels"] if x != name]
            return json.dumps(
                {"data": {"removeLabelsFromLabelable": {"clientMutationId": None}}}
            )
        if "addComment" in query:
            cid = self._next_comment_id
            self._next_comment_id += 1
            issue = self._by_node_id(variables["id"])
            ((repo, number),) = [k for k, v in self.issues.items() if v is issue]
            self.comments[cid] = {
                "repo": repo,
                "number": number,
                "body": variables["body"],
            }
            return json.dumps(
                {"data": {"addComment": {"commentEdge": {"node": {"databaseId": cid}}}}}
            )
        if "deleteIssueComment" in query:
            cid = int(variables["id"].split(":", 1)[1])
            del self.comments[cid]
            return json.dumps(
                {"data": {"deleteIssueComment": {"clientMutationId": None}}}
            )
        if "reopenIssue" in query:
            issue = self._by_node_id(variables["id"])
            issue["state"], issue["state_reason"] = "open", "reopened"
            return json.dumps(
                {"data": {"reopenIssue": {"issue": {"id": variables["id"]}}}}
            )
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
            return json.dumps(
                {"data": {"closeIssue": {"issue": {"id": variables["id"]}}}}
            )
        raise AssertionError(f"unexpected graphql query: {query}")
