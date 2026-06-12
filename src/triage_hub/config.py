"""Load tenant configuration (repo scope)."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.name}"


def load_repos(path: str | pathlib.Path) -> list[Repo]:
    data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    entries = data.get("repositories") or []
    repos: list[Repo] = []
    for entry in entries:
        owner, sep, name = str(entry).partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError(f"invalid repo entry: {entry!r} (expected owner/name)")
        repos.append(Repo(owner, name))
    return repos
