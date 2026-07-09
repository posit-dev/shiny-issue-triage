"""Shared helper for appending records to weekly-partitioned JSONL logs."""

from __future__ import annotations

import json
import pathlib
from datetime import date


def append_weekly(
    records: list[dict], base_dir: str | pathlib.Path, *, today: str | None = None
) -> pathlib.Path:
    day = date.fromisoformat(today) if today else date.today()
    year, week, _ = day.isocalendar()
    out = pathlib.Path(base_dir) / f"{year}" / f"W{week:02d}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = out.read_text(encoding="utf-8") if out.exists() else ""
    payload = existing + "".join(json.dumps(r) + "\n" for r in records)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(out)
    return out
