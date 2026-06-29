from triage_verse import db


def test_vector_upsert_and_hash(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert db.get_embed_hash(con, "r/r", 1) is None
    vec = [1.0, 0.0, 0.0] + [0.0] * (db.VEC_DIM - 3)
    db.upsert_vector(con, "r/r", 1, "h1", vec)
    assert db.get_embed_hash(con, "r/r", 1) == "h1"
    # re-upsert updates hash, not row count
    db.upsert_vector(con, "r/r", 1, "h2", vec)
    assert db.get_embed_hash(con, "r/r", 1) == "h2"
    assert con.execute("SELECT COUNT(*) FROM issue_vectors").fetchone()[0] == 1


def test_knn_orders_by_cosine_distance(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    pad = [0.0] * (db.VEC_DIM - 3)
    db.upsert_vector(con, "r/a", 1, "h", [1.0, 0.0, 0.0] + pad)
    db.upsert_vector(con, "r/b", 2, "h", [0.9, 0.1, 0.0] + pad)  # near a
    db.upsert_vector(con, "r/c", 3, "h", [0.0, 0.0, 1.0] + pad)  # orthogonal
    hits = db.knn(con, [1.0, 0.0, 0.0] + pad, k=3)
    assert hits[0][:2] == ("r/a", 1)
    assert hits[1][:2] == ("r/b", 2)
    # cosine_sim of the orthogonal vector ~ 0 -> distance ~ 1
    assert 1 - hits[2][2] < 0.1
