# tests/triage_verse/test_db_migrate.py
import sqlite3

from triage_verse import db


def _old_schema_db(path):
    """A mirror created with the pre-#20 decision-table PKs and one row each."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE classifications (
          repo TEXT NOT NULL, number INTEGER NOT NULL, clf_hash TEXT NOT NULL,
          type TEXT NOT NULL, priority TEXT NOT NULL, assessment TEXT NOT NULL,
          labels_json TEXT NOT NULL DEFAULT '[]', close_candidate_json TEXT,
          confidence REAL NOT NULL, model TEXT NOT NULL, run_id TEXT NOT NULL,
          at TEXT NOT NULL, PRIMARY KEY (repo, number)
        );
        CREATE TABLE dedup_verdicts (
          repo_a TEXT NOT NULL, number_a INTEGER NOT NULL,
          repo_b TEXT NOT NULL, number_b INTEGER NOT NULL,
          hash_a TEXT NOT NULL, hash_b TEXT NOT NULL, verdict TEXT NOT NULL,
          canonical_json TEXT, cross_repo_option TEXT, confidence REAL NOT NULL,
          rationale TEXT NOT NULL, model TEXT NOT NULL, run_id TEXT NOT NULL,
          at TEXT NOT NULL,
          PRIMARY KEY (repo_a, number_a, repo_b, number_b)
        );
        """
    )
    con.execute(
        "INSERT INTO classifications VALUES "
        "('r/r',1,'h1','fix','High','actionable','[]',NULL,0.9,"
        "'claude-haiku-4-5','runOld','2026-06-01T00:00:00Z')"
    )
    con.execute(
        "INSERT INTO dedup_verdicts VALUES "
        "('r/a',1,'r/b',2,'ha','hb','duplicate','\"r/a#1\"','close-and-link',"
        "0.8,'same','claude-sonnet-5','runOld','2026-06-01T00:00:00Z')"
    )
    con.commit()
    con.close()


def _pk_cols(con, table):
    return {r["name"]: r["pk"] for r in con.execute(f"PRAGMA table_info({table})")}


def test_migrate_preserves_rows_and_widens_pk(tmp_path):
    path = tmp_path / "m.sqlite"
    _old_schema_db(path)

    con = db.connect(path)  # runs _migrate

    assert _pk_cols(con, "classifications")["run_id"] > 0
    assert _pk_cols(con, "dedup_verdicts")["run_id"] > 0
    assert db.get_classification(con, "r/r", 1)["run_id"] == "runOld"
    assert db.get_dedup_verdict(con, "r/a", 1, "r/b", 2)["run_id"] == "runOld"
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "m.sqlite"
    _old_schema_db(path)
    db.connect(path).close()
    con = db.connect(path)  # second connect must be a no-op
    assert db.get_classification(con, "r/r", 1)["run_id"] == "runOld"
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1


def test_fresh_db_has_new_pk_and_version(tmp_path):
    con = db.connect(tmp_path / "fresh.sqlite")
    assert _pk_cols(con, "classifications")["run_id"] > 0
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
