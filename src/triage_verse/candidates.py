"""Local duplicate-candidate retrieval over the embedding index."""

from __future__ import annotations

import struct

from . import db


def _canonical(a: tuple, b: tuple) -> tuple:
    return (a, b) if (a[0], a[1]) <= (b[0], b[1]) else (b, a)


def _vector_of(con, repo: str, number: int) -> list[float]:
    """Read a stored embedding back as a list of floats.

    sqlite_vec.deserialize_float32 is absent in the installed version (<0.1.6),
    so we deserialise the raw IEEE-754 little-endian blob with struct.unpack.
    """
    row = con.execute(
        "SELECT v.embedding AS e FROM vec_issues v "
        "JOIN issue_vectors iv ON iv.id=v.rowid WHERE iv.repo=? AND iv.number=?",
        (repo, number),
    ).fetchone()
    blob: bytes = row["e"]
    n = len(blob) // 4
    return list(struct.unpack_from(f"<{n}f", blob))


def candidate_pairs(
    con, cfg, *, repo: str | None = None, limit: int | None = None
) -> list[tuple[tuple[str, int, str], tuple[str, int, str]]]:
    """Return canonical-ordered duplicate-candidate pairs for open issues.

    Each element is ``((repo_a, number_a, hash_a), (repo_b, number_b, hash_b))``
    with:

    * self-matches removed
    * neighbours below ``cfg.cosine_threshold`` removed
    * pair order canonicalised (lexicographic on repo then number)
    * de-duplicated (each pair appears at most once)
    * pairs already in ``dedup_verdicts`` with unchanged hashes skipped
    """
    where = "WHERE i.is_pr=0 AND i.state='OPEN'" + (" AND i.repo=:repo" if repo else "")
    sql = (
        "SELECT i.repo AS repo, i.number AS number, iv.embed_hash AS h "
        "FROM issues i JOIN issue_vectors iv ON iv.repo=i.repo AND iv.number=i.number "
        f"{where} ORDER BY i.repo, i.number"
    )
    open_rows = con.execute(sql, {"repo": repo} if repo else {}).fetchall()
    if limit is not None:
        open_rows = open_rows[:limit]

    seen: set[tuple] = set()
    pairs: list[tuple] = []
    for row in open_rows:
        src = (row["repo"], row["number"], row["h"])
        query_vec = _vector_of(con, row["repo"], row["number"])
        for nbr_repo, nbr_num, distance in db.knn(con, query_vec, cfg.candidate_top_k):
            if (nbr_repo, nbr_num) == (row["repo"], row["number"]):
                continue
            if (1.0 - distance) < cfg.cosine_threshold:
                continue
            nbr_hash = db.get_embed_hash(con, nbr_repo, nbr_num)
            a, b = _canonical(src, (nbr_repo, nbr_num, nbr_hash))
            key = (a[0], a[1], b[0], b[1])
            if key in seen:
                continue
            seen.add(key)
            cached = db.get_dedup_verdict(con, a[0], a[1], b[0], b[1])
            if cached and cached["hash_a"] == a[2] and cached["hash_b"] == b[2]:
                continue
            pairs.append((a, b))
    return pairs
