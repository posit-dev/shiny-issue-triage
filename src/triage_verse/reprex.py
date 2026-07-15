"""Reprex bot: mark an issue for an AI-generated minimal-reprex attempt.

Applying the label is the only local action. A (dormant) GitHub workflow picks
the label up and runs a read/repro session that appends a minimal-reprex comment
to the issue (append-only: it never edits existing content). It never auto-closes:
a non-reproducible issue is labelled ai-triage:no-reprex + ai-triage:needs-review
and routed back to the human review queue (high-stakes closes are never automated).
"""

from __future__ import annotations

from typing import Callable

LABEL = "ai-triage:needs-reprex"


def request_reprex(
    repo: str, number: int, *, run_gh: Callable[..., str], label: str = LABEL
) -> None:
    run_gh(["issue", "edit", str(number), "--repo", repo, "--add-label", label])
