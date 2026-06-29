"""Publish/bootstrap mirror snapshots as GitHub Release assets.

A rolling `mirror-latest` release is refreshed after every successful run;
dated `mirror-YYYY-MM-DD` releases are restore points (keep the newest N).
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
from datetime import date
from typing import Callable

import zstandard

from .gh import GhError, run_gh

ASSET_NAME = "mirror.sqlite.zst"
LATEST_TAG = "mirror-latest"


class SnapshotError(RuntimeError):
    pass


def vacuum_to(db_path: str | pathlib.Path, out_path: str | pathlib.Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("VACUUM INTO ?", (str(out_path),))
    finally:
        con.close()


def compress(src: str | pathlib.Path, dst: str | pathlib.Path) -> None:
    cctx = zstandard.ZstdCompressor(level=9)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        cctx.copy_stream(fin, fout)


def decompress(src: str | pathlib.Path, dst: str | pathlib.Path) -> None:
    dctx = zstandard.ZstdDecompressor()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        dctx.copy_stream(fin, fout)


def _ensure_release(tag: str, gh_run: Callable) -> None:
    try:
        gh_run(["release", "view", tag])
    except GhError:
        gh_run(["release", "create", tag, "--title", tag,
                "--notes", "triage-verse mirror snapshot", "--latest=false"])


def _prune_dated(gh_run: Callable, keep: int) -> None:
    out = gh_run(["release", "list", "--limit", "100", "--json", "tagName"])
    tags = [r["tagName"] for r in json.loads(out)
            if r["tagName"].startswith("mirror-") and r["tagName"] != LATEST_TAG]
    for tag in sorted(tags, reverse=True)[keep:]:
        gh_run(["release", "delete", tag, "--yes", "--cleanup-tag"])


def publish(db_path: str | pathlib.Path, *, gh_run: Callable = run_gh,
            dated: bool = False, today: str | None = None, keep: int = 8) -> str:
    if not pathlib.Path(db_path).exists():
        raise SnapshotError(
            f"{db_path} does not exist; run `triage-verse sync` first")
    with tempfile.TemporaryDirectory() as tmp:
        plain = pathlib.Path(tmp) / "mirror.sqlite"
        packed = pathlib.Path(tmp) / ASSET_NAME
        vacuum_to(db_path, plain)
        compress(plain, packed)

        _ensure_release(LATEST_TAG, gh_run)
        gh_run(["release", "upload", LATEST_TAG, str(packed), "--clobber"])

        if dated:
            day = today or date.today().isoformat()
            tag = f"mirror-{day}"
            _ensure_release(tag, gh_run)
            gh_run(["release", "upload", tag, str(packed), "--clobber"])
            _prune_dated(gh_run, keep)
            return tag
    return LATEST_TAG


def bootstrap(db_path: str | pathlib.Path, *, gh_run: Callable = run_gh,
              force: bool = False) -> None:
    db_path = pathlib.Path(db_path)
    if db_path.exists() and not force:
        raise SnapshotError(f"{db_path} exists; pass --force to overwrite")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Stage on the same volume as the target so the final replace is atomic;
    # a failed download/decompress never corrupts an existing mirror.
    with tempfile.TemporaryDirectory(dir=db_path.parent) as tmp:
        packed = pathlib.Path(tmp) / ASSET_NAME
        gh_run(["release", "download", LATEST_TAG, "--pattern", ASSET_NAME,
                "--output", str(packed)])
        staged = pathlib.Path(tmp) / "mirror_new.sqlite"
        decompress(packed, staged)
        staged.replace(db_path)
