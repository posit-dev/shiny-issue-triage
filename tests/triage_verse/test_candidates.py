# tests/triage_verse/test_candidates.py
from triage_verse import candidates, config, db, embed


def _cfg(top_k=10, thr=0.80):
    return config.ModelsConfig(
        embed_model="m",
        embed_dim=db.VEC_DIM,
        candidate_top_k=top_k,
        cosine_threshold=thr,
        classify=config.StageConfig("claude-haiku-4-5", 512),
        recheck=config.StageConfig("claude-sonnet-5", 1024, 0.7),
        dedup=config.StageConfig("claude-sonnet-5", 1024),
        max_requests_per_batch=500,
        poll_interval_seconds=30,
        batch_only=True,
        max_usd_per_day=50,
        pricing={},
    )


def _open_issue(con, repo, number, title, body):
    con.execute(
        "INSERT INTO issues (repo, number, title, body, state, created_at, updated_at,"
        " is_pr) VALUES (?, ?, ?, ?, 'OPEN', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z', 0)",
        (repo, number, title, body),
    )


def test_candidate_pairs_are_canonical_and_thresholded(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _emb = embed.FakeEmbedder(dim=db.VEC_DIM)
    pad = [0.0] * (db.VEC_DIM - 3)
    # two near-identical vectors, one far
    _open_issue(con, "r/a", 1, "x", "x")
    db.upsert_vector(con, "r/a", 1, "h1", [1.0, 0.0, 0.0] + pad)
    _open_issue(con, "r/b", 2, "y", "y")
    db.upsert_vector(con, "r/b", 2, "h2", [0.99, 0.01, 0.0] + pad)
    _open_issue(con, "r/c", 3, "z", "z")
    db.upsert_vector(con, "r/c", 3, "h3", [0.0, 0.0, 1.0] + pad)
    con.commit()

    pairs = candidates.candidate_pairs(con, _cfg())
    refs = {(a[:2], b[:2]) for a, b in pairs}
    assert (("r/a", 1), ("r/b", 2)) in refs
    assert not any("r/c" in (a[0], b[0]) for a, b in pairs)  # orthogonal filtered out


def test_candidate_pairs_skip_cached_unchanged(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    pad = [0.0] * (db.VEC_DIM - 3)
    _open_issue(con, "r/a", 1, "x", "x")
    db.upsert_vector(con, "r/a", 1, "h1", [1.0, 0.0, 0.0] + pad)
    _open_issue(con, "r/b", 2, "y", "y")
    db.upsert_vector(con, "r/b", 2, "h2", [0.99, 0.01, 0.0] + pad)
    con.commit()
    db.upsert_dedup_verdict(
        con,
        {
            "repo_a": "r/a",
            "number_a": 1,
            "repo_b": "r/b",
            "number_b": 2,
            "hash_a": "h1",
            "hash_b": "h2",
            "verdict": "distinct",
            "canonical_json": None,
            "cross_repo_option": None,
            "confidence": 0.9,
            "rationale": "no",
            "model": "claude-sonnet-5",
            "run_id": "r",
            "at": "2026-01-01T00:00:00Z",
        },
    )
    assert candidates.candidate_pairs(con, _cfg()) == []
