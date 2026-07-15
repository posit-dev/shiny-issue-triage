"""Tier 2: mark an issue for an AI draft-PR fix attempt."""

from __future__ import annotations

from typing import Callable

from . import gh as gh_mod

LABEL = "ai-triage:fix-requested"


def request_fix(
    repo: str, number: int, *, run_gh: Callable[..., str], label: str = LABEL
) -> None:
    gh_mod.add_issue_label(repo, number, label, run_gh=run_gh)
