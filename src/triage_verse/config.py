"""Load tenant configuration (repo scope)."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

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
    if not isinstance(entries, list):
        raise ValueError(f"'repositories' must be a list, got {type(entries).__name__}")
    repos: list[Repo] = []
    for entry in entries:
        owner, sep, name = str(entry).partition("/")
        if not sep or not owner or not name or "/" in name:
            raise ValueError(f"invalid repo entry: {entry!r} (expected owner/name)")
        repos.append(Repo(owner, name))
    return repos


@dataclass(frozen=True)
class StageConfig:
    model: str
    max_tokens: int
    confidence_floor: float | None = None


@dataclass(frozen=True)
class TiersConfig:
    tier1_max_per_day: int = 25
    tier2_max_per_week: int = 10


@dataclass(frozen=True)
class AutonomyConfig:
    min_decisions: int = 200
    min_precision: float = 0.98
    confidence_floor: float = 0.9
    audit_rate: float = 0.10


@dataclass(frozen=True)
class ModelsConfig:
    embed_model: str
    embed_dim: int
    candidate_top_k: int
    cosine_threshold: float
    classify: StageConfig
    recheck: StageConfig
    dedup: StageConfig
    max_requests_per_batch: int
    poll_interval_seconds: int
    batch_only: bool
    max_usd_per_day: float
    pricing: dict[str, dict[str, float]]
    backend: str = "claude_cli"
    workers: int = 1
    tiers: TiersConfig = TiersConfig()
    autonomy: AutonomyConfig = AutonomyConfig()


def _stage(d: dict[str, Any]) -> StageConfig:
    return StageConfig(
        model=d["model"],
        max_tokens=d["max_tokens"],
        confidence_floor=d.get("confidence_floor"),
    )


def load_models_config(path: str | pathlib.Path) -> ModelsConfig:
    data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    emb, st, b, sp = data["embedding"], data["stages"], data["batch"], data["spend"]
    t = data.get("tiers") or {}
    a = data.get("autonomy") or {}
    return ModelsConfig(
        embed_model=emb["model"],
        embed_dim=emb["dim"],
        candidate_top_k=emb["candidate_top_k"],
        cosine_threshold=emb["cosine_threshold"],
        classify=_stage(st["classify"]),
        recheck=_stage(st["recheck"]),
        dedup=_stage(st["dedup"]),
        max_requests_per_batch=b["max_requests_per_batch"],
        poll_interval_seconds=b["poll_interval_seconds"],
        batch_only=sp["batch_only"],
        max_usd_per_day=sp["max_usd_per_day"],
        pricing=sp["pricing"],
        backend=data.get("backend", "claude_cli"),
        workers=b.get("workers", 1),
        tiers=TiersConfig(**t),
        autonomy=AutonomyConfig(**a),
    )
