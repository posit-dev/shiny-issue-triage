"""Stateful in-memory fake for gh.run_gh, covering the executor's call surface."""

from __future__ import annotations

import json
import re


class FakeGh:
    """Callable standing in for gh.run_gh. Issues keyed by (repo, number)."""

    def __init__(self, issues: dict[tuple[str, int], dict]):
        # Each issue dict: labels (list[str]), state ("open"/"closed"),
        # state_reason (str|None), updated_at (str), node_id (str).
        self.issues = {k: dict(v) for k, v in issues.items()}
        self.comments: dict[int, dict] = {}  # comment id -> {repo, number, body}
        self._next_comment_id = 1000
        self.mutating_calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs) -> str:
        if args[0] == "api":
            return self._api(args)
        if args[0] == "issue":
            return self._issue_cmd(args)
        raise AssertionError(f"unexpected gh args: {args}")

    # -- helpers ---------------------------------------------------------

    def _find(self, repo: str, number: int) -> dict:
        return self.issues[(repo, number)]

    def _api(self, args: list[str]) -> str:
        if args[1] == "graphql":
            return self._graphql(args)
        if "-X" in args and "DELETE" in args:
            path = args[-1]
            m = re.match(r"repos/([\w.-]+/[\w.-]+)/issues/comments/(\d+)$", path)
            assert m, path
            self.mutating_calls.append(args)
            del self.comments[int(m.group(2))]
            return ""
        path = args[1]
        m = re.match(r"repos/([\w.-]+/[\w.-]+)/issues/(\d+)/comments$", path)
        if m:  # POST comment: gh api repos/.../comments -f body=...
            self.mutating_calls.append(args)
            body = next(
                a[len("body=") :] for a in args if a.startswith("body=")
            )
            cid = self._next_comment_id
            self._next_comment_id += 1
            self.comments[cid] = {
                "repo": m.group(1),
                "number": int(m.group(2)),
                "body": body,
            }
            issue = self._find(m.group(1), int(m.group(2)))
            issue["updated_at"] = issue["updated_at"] + "+c"
            return json.dumps({"id": cid})
        m = re.match(r"repos/([\w.-]+/[\w.-]+)/issues/(\d+)$", path)
        assert m, path
        issue = self._find(m.group(1), int(m.group(2)))
        return json.dumps(
            {
                "updated_at": issue["updated_at"],
                "node_id": issue["node_id"],
                "state": issue["state"],
                "state_reason": issue["state_reason"],
                "labels": [{"name": name} for name in issue["labels"]],
            }
        )

    def _graphql(self, args: list[str]) -> str:
        # closeIssue(stateReason: DUPLICATE, duplicateIssueId: ...)
        self.mutating_calls.append(args)
        fields = dict(
            a.split("=", 1) for a in args if "=" in a and not a.startswith("query=")
        )
        target = self._by_node_id(fields["issue"])
        assert self._by_node_id(fields["dup"]) is not None
        target["state"] = "closed"
        target["state_reason"] = "duplicate"
        return json.dumps({"data": {"closeIssue": {"issue": {"id": fields["issue"]}}}})

    def _by_node_id(self, node_id: str) -> dict:
        for issue in self.issues.values():
            if issue["node_id"] == node_id:
                return issue
        raise AssertionError(f"unknown node id {node_id}")

    def _issue_cmd(self, args: list[str]) -> str:
        self.mutating_calls.append(args)
        number = int(args[2])
        repo = args[args.index("--repo") + 1]
        issue = self._find(repo, number)
        if args[1] == "edit":
            for flag, value in zip(args, args[1:]):
                if flag == "--add-label" and value not in issue["labels"]:
                    issue["labels"] = [*issue["labels"], value]
                if flag == "--remove-label" and value in issue["labels"]:
                    issue["labels"] = [x for x in issue["labels"] if x != value]
        elif args[1] == "close":
            issue["state"] = "closed"
            reason = args[args.index("--reason") + 1]
            issue["state_reason"] = "completed" if reason == "completed" else "not_planned"
        elif args[1] == "reopen":
            issue["state"] = "open"
            issue["state_reason"] = "reopened"
        else:
            raise AssertionError(f"unexpected issue subcommand: {args}")
        issue["updated_at"] = issue["updated_at"] + "+m"
        return ""
