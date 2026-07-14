"""Tier 2: mark an issue for an AI draft-PR fix attempt."""

from __future__ import annotations

from typing import Callable

LABEL = "ai-triage:fix-requested"


def request_fix(
    repo: str, number: int, *, run_gh: Callable[..., str], label: str = LABEL
) -> None:
    run_gh(["issue", "edit", str(number), "--repo", repo, "--add-label", label])
