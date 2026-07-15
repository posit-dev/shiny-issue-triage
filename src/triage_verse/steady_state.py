"""Ordered stage runner for the steady-state loop (sync/analyze/tier1/state/snapshot)."""

from __future__ import annotations

from typing import Callable


def run(
    stages: list[tuple[str, Callable[[], None]]], *, log: Callable[[str], None] = print
) -> dict:
    completed: list[str] = []
    for name, fn in stages:
        log(f"stage: {name} - starting")
        try:
            fn()
        except Exception as exc:  # a failed stage stops the loop; no rollback
            log(f"stage: {name} - FAILED: {exc}")
            return {"completed": completed, "failed": name, "error": str(exc)}
        completed.append(name)
        log(f"stage: {name} - done")
    return {"completed": completed, "failed": None}
