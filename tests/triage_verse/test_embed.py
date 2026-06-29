# tests/triage_verse/test_embed.py
from triage_verse import db, embed


def _seed_issue(con, repo, number, title, body, is_pr=0):
    con.execute(
        "INSERT INTO issues (repo, number, title, body, state, created_at, updated_at,"
        " is_pr) VALUES (?, ?, ?, ?, 'OPEN', '2026-01-01T00:00:00Z',"
        " '2026-01-01T00:00:00Z', ?)",
        (repo, number, title, body, is_pr),
    )
    con.commit()


def test_embed_repo_embeds_changed_issues_only(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_issue(con, "r/r", 1, "crash on save", "stack trace here")
    _seed_issue(con, "r/r", 2, "feature please", "would be nice")
    emb = embed.FakeEmbedder(dim=db.VEC_DIM)

    assert embed.embed_repo(con, "r/r", emb) == 2
    # second run: nothing changed -> 0 re-embeds
    assert embed.embed_repo(con, "r/r", emb) == 0

    # mutate body -> hash changes -> re-embed just that one
    con.execute("UPDATE issues SET body='new trace' WHERE number=1")
    con.commit()
    assert embed.embed_repo(con, "r/r", emb) == 1


def test_embed_repo_skips_prs(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_issue(con, "r/r", 5, "a pr", "diff", is_pr=1)
    assert embed.embed_repo(con, "r/r", emb := embed.FakeEmbedder(dim=db.VEC_DIM)) == 0  # noqa: F841


def test_fake_embedder_is_deterministic_and_right_dim():
    emb = embed.FakeEmbedder(dim=8)
    a = emb.embed(["hello"])[0]
    b = emb.embed(["hello"])[0]
    assert a == b and len(a) == 8
