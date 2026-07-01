import pathlib

from triage_verse import analyze, config, db, embed, llm

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUBRIC = REPO_ROOT / ".github" / "triage" / "issue-triage-rubric.md"
LABELS = REPO_ROOT / ".github" / "triage" / "labels.yaml"


def _cfg(cap=50.0):
    return config.ModelsConfig(
        "m",
        db.VEC_DIM,
        10,
        0.80,
        config.StageConfig("claude-haiku-4-5", 512),
        config.StageConfig("claude-sonnet-5", 1024, 0.70),
        config.StageConfig("claude-sonnet-5", 1024),
        500,
        0,
        True,
        cap,
        {
            "claude-haiku-4-5": {"input": 0.5, "cached": 0.05, "output": 2.5},
            "claude-sonnet-5": {"input": 1.5, "cached": 0.15, "output": 7.5},
        },
    )


def _two_similar_issues(con):
    pad = [0.0] * (db.VEC_DIM - 3)
    for repo, num, v in (("r/a", 1, [1.0, 0.0, 0.0]), ("r/b", 2, [0.99, 0.01, 0.0])):
        con.execute(
            "INSERT INTO issues (repo, number, title, body, state, created_at,"
            " updated_at, is_pr) VALUES (?, ?, 'crash', 'trace', 'OPEN',"
            " '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z', 0)",
            (repo, num),
        )
        db.upsert_vector(con, repo, num, "h" + str(num), v + pad)
    con.commit()


def test_analyze_runs_full_pipeline_and_writes_proposals(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _two_similar_issues(con)
    # Haiku says low-confidence -> forces a recheck wave; Sonnet finalizes.
    scripted = {
        "c0": {"status": "succeeded", "payload": _clf(conf=0.5)},
        "c1": {"status": "succeeded", "payload": _clf(conf=0.9)},
        "r0": {"status": "succeeded", "payload": _clf(conf=0.95)},
        "d0": {
            "status": "succeeded",
            "payload": {
                "verdict": "duplicate",
                "canonical": "r/a#1",
                "cross_repo_option": "close-and-link",
                "confidence": 0.9,
                "rationale": "same",
            },
        },
    }
    fake = llm.FakeBatchClient(scripted)
    summary = analyze.analyze(
        con,
        _cfg(),
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=fake,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )

    assert summary["classified"] == 2
    assert summary["rechecked"] == 1
    assert summary["pairs"] == 1
    assert (
        db.get_classification(con, "r/a", 1)["model"] == "claude-sonnet-5"
    )  # rechecked
    assert db.get_dedup_verdict(con, "r/a", 1, "r/b", 2)["verdict"] == "duplicate"
    assert con.execute("SELECT COUNT(*) FROM spend").fetchone()[0] == 4
    # proposals file exists and is non-empty
    files = list((tmp_path / "proposals").rglob("*.jsonl"))
    assert files and files[0].read_text().strip()


def test_breaker_stops_submitting(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _two_similar_issues(con)
    db.insert_spend(con, "old", "classify", "claude-haiku-4-5", 0, 0, 0, 100.0)
    fake = llm.FakeBatchClient({})
    summary = analyze.analyze(
        con,
        _cfg(cap=1.0),
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=fake,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )
    assert summary["halted_on_budget"] is True
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0


def test_analyze_resumes_without_resubmitting(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _two_similar_issues(con)
    scripted = {
        f"c{i}": {"status": "succeeded", "payload": _clf(0.9)} for i in range(2)
    }
    scripted["d0"] = {
        "status": "succeeded",
        "payload": {
            "verdict": "distinct",
            "canonical": None,
            "cross_repo_option": None,
            "confidence": 0.9,
            "rationale": "x",
        },
    }

    class _Pending(llm.FakeBatchClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.ready = False

        def status(self, pid):
            return "ended" if self.ready else "in_progress"

    client = _Pending(scripted)
    analyze.analyze(
        con,
        _cfg(),
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=False,
        sleep=lambda s: None,
    )
    assert (
        con.execute("SELECT COUNT(*) FROM batches WHERE status='submitted'").fetchone()[
            0
        ]
        == 2
    )
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0

    client.ready = True
    analyze.analyze(
        con,
        _cfg(),
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )
    # no new classify/dedup batches were created on resume
    assert (
        con.execute(
            "SELECT COUNT(*) FROM batches WHERE stage IN ('classify','dedup')"
        ).fetchone()[0]
        == 2
    )
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 2


def _clf(conf):
    return {
        "type": "fix",
        "priority": "High",
        "assessment": "actionable",
        "labels": [],
        "close_candidate": None,
        "confidence": conf,
    }


class _SyncFakeClient(llm.FakeBatchClient):
    synchronous = True


def _n_issues(con, n):
    # Distinct titles/bodies so FakeEmbedder (content-hash-derived) produces
    # mutually dissimilar vectors and no dedup candidate pairs form -- these
    # tests are about the classify stage only, and stray dedup submissions
    # would throw off the exact assertions.
    for i in range(n):
        con.execute(
            "INSERT INTO issues (repo, number, title, body, state, created_at,"
            " updated_at, is_pr) VALUES ('r/a', ?, ?, ?, 'OPEN',"
            " '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z', 0)",
            (i + 1, f"issue {i + 1}", f"unrelated body content {i + 1}"),
        )
    con.commit()


def test_breaker_trips_mid_stage_not_just_between_stages(tmp_path):
    # Each classify item costs exactly $1.00 via usd_for_usage: the default
    # _FakeUsage is (input=10, cached=0, output=5); rig pricing so output
    # tokens alone drive the cost to a round $1.00/item (5 * 200000/1e6).
    # breaker_tripped fires once *already recorded* spend >= cap; with a
    # $2.00 cap that happens right after item 2 ($2.00 spent), so the check
    # before item 3 halts the stage -- exactly 2 items get through.
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 5)
    cfg = _cfg(cap=2.0)
    cfg.pricing["claude-haiku-4-5"] = {
        "input": 0.0,
        "cached": 0.0,
        "output": 200_000.0,
    }
    scripted = {
        f"c{i}": {"status": "succeeded", "payload": _clf(0.9)} for i in range(5)
    }
    client = _SyncFakeClient(scripted)
    summary = analyze.analyze(
        con,
        cfg,
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )
    assert summary["halted_on_budget"] is True
    assert summary["classified"] == 2
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM spend").fetchone()[0] == 2


def test_synchronous_client_persists_each_item_before_next(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 3)
    scripted = {
        f"c{i}": {"status": "succeeded", "payload": _clf(0.9)} for i in range(3)
    }
    client = _SyncFakeClient(scripted)
    analyze.analyze(
        con,
        _cfg(),
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )
    rows = con.execute("SELECT status FROM batches WHERE stage='classify'").fetchall()
    assert len(rows) == 3
    assert all(r["status"] == "collected" for r in rows)
