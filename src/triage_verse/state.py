"""Sync append-only JSONL state + cursors with the triage-state branch."""

from __future__ import annotations

STATE_FILES = ("proposals", "decisions", "results")
CURSORS_FILE = "cursors.json"


def _lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def union_merge_lines(existing: str, incoming: str) -> str:
    """Existing lines plus incoming lines not already present, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for ln in _lines(existing) + _lines(incoming):
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return "".join(ln + "\n" for ln in out)
