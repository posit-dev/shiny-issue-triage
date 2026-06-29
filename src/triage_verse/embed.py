"""Local issue embeddings: interface, deterministic fake, fastembed impl, stage."""

from __future__ import annotations

import hashlib
import struct
from typing import Protocol

from . import db


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    """Deterministic pseudo-embeddings from a content hash. No model, no network."""

    def __init__(self, dim: int = db.VEC_DIM) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = (digest * (self.dim * 4 // len(digest) + 1))[: self.dim * 4]
            vec = [
                struct.unpack_from("<i", raw, 4 * i)[0] / 2**31 for i in range(self.dim)
            ]
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


class FastEmbedEmbedder:
    """Real embeddings via fastembed (ONNX). Imported lazily to keep tests light."""

    def __init__(self, model: str) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]


def embed_hash(title: str, body: str | None) -> str:
    return hashlib.sha256((title + "\n" + (body or "")).encode("utf-8")).hexdigest()


def embed_repo(con, repo: str, embedder: Embedder, *, full: bool = False) -> int:
    rows = con.execute(
        "SELECT number, title, body FROM issues WHERE repo=? AND is_pr=0", (repo,)
    ).fetchall()
    pending = []
    for r in rows:
        h = embed_hash(r["title"], r["body"])
        if full or db.get_embed_hash(con, repo, r["number"]) != h:
            pending.append((r["number"], r["title"], r["body"], h))
    if not pending:
        return 0
    vectors = embedder.embed([f"{t}\n{b or ''}" for _, t, b, _ in pending])
    for (number, _, _, h), vec in zip(pending, vectors):
        db.upsert_vector(con, repo, number, h, vec)
    con.commit()
    return len(pending)
