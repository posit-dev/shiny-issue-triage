"""Thin wrapper around the `gh` CLI (auth and HTTP handled by gh)."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Callable

RETRYABLE_MARKERS = ("rate limit", "HTTP 429", "HTTP 502", "HTTP 503",
                     "HTTP 504", "timeout")


class GhError(RuntimeError):
    pass


def run_gh(args: list[str], *, input: str | None = None, retries: int = 5,
           sleep: Callable[[float], None] = time.sleep) -> str:
    delay = 30.0
    last_error = "gh failed"
    for attempt in range(retries):
        try:
            proc = subprocess.run(["gh", *args], capture_output=True,
                                  text=True, encoding="utf-8", input=input)
        except FileNotFoundError:
            raise GhError("gh binary not found — install the GitHub CLI"
                          " (https://cli.github.com)") from None
        if proc.returncode == 0:
            return proc.stdout
        last_error = proc.stderr.strip() or f"gh exited {proc.returncode}"
        retryable = any(marker.lower() in last_error.lower()
                        for marker in RETRYABLE_MARKERS)
        if not retryable:
            raise GhError(last_error)
        if attempt < retries - 1:
            sleep(delay)
            delay *= 2
    raise GhError(last_error)


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
