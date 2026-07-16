import json
import pathlib
import threading
import time

import pytest

from triage_verse import analyze, config, db, embed, llm

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUBRIC = REPO_ROOT / ".github" / "triage" / "issue-triage-rubric.md"
LABELS = REPO_ROOT / ".github" / "triage" / "labels.yaml"


def _cfg(cap=50.0, workers=1):
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
        workers=workers,
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


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Usage:
    def __init__(self):
        self.input_tokens = 10
        self.cache_read_input_tokens = 0
        self.output_tokens = 5


class _Msg:
    def __init__(self, payload):
        self.content = [_Block(json.dumps(payload))]
        self.usage = _Usage()


class _ParallelFakeClient:
    """Exposes only submit_one -- the worker-pool primitive. Tracks how many
    calls were simultaneously in flight, to prove real concurrency occurred
    (a non-flaky alternative to asserting on wall-clock timing)."""

    synchronous = True

    def __init__(self, scripted, delay=0.05):
        self.scripted = scripted
        self.delay = delay
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def submit_one(self, request):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        spec = self.scripted[request.custom_id]
        return llm.BatchResult(request.custom_id, "succeeded", message=_Msg(spec))


def test_parallel_dispatch_runs_up_to_workers_items_concurrently(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 4)
    cfg = _cfg(workers=2)
    scripted = {f"c{i}": _clf(0.9) for i in range(4)}
    client = _ParallelFakeClient(scripted, delay=0.05)

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

    assert summary["classified"] == 4
    assert client.max_active == 2  # exactly the worker limit -- proves real overlap
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 4
    rows = con.execute("SELECT status FROM batches WHERE stage='classify'").fetchall()
    assert len(rows) == 4 and all(r["status"] == "collected" for r in rows)


def test_parallel_breaker_blocks_all_dispatch_when_already_over_budget(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 3)
    db.insert_spend(con, "old", "classify", "claude-haiku-4-5", 0, 0, 0, 100.0)
    cfg = _cfg(cap=1.0, workers=2)
    scripted = {f"c{i}": _clf(0.9) for i in range(3)}
    client = _ParallelFakeClient(scripted, delay=0.01)

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
    assert summary["classified"] == 0
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 0


def test_parallel_breaker_bounds_overshoot_by_worker_count(tmp_path):
    # Each classify item costs exactly $1.00 (same pricing rig as the
    # sequential breaker test). With a $2.0 cap: the breaker only trips once
    # *already-recorded* spend >= $2.0, which needs at least 2 completed
    # items ($2.00). With workers=2, at most 1 extra item can already be in
    # flight at the moment the 2nd completion crosses the cap (since at most
    # `workers` items are ever in flight at once) -- so completed count is
    # bounded to [2, 2 + (workers - 1)] = [2, 3].
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 5)
    cfg = _cfg(cap=2.0, workers=2)
    cfg.pricing["claude-haiku-4-5"] = {
        "input": 0.0,
        "cached": 0.0,
        "output": 200_000.0,
    }
    scripted = {f"c{i}": _clf(0.9) for i in range(5)}
    client = _ParallelFakeClient(scripted, delay=0.02)

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
    assert 2 <= summary["classified"] <= 3
    assert summary["classified"] < 5  # the breaker had a real effect


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


def test_parallel_resume_after_crash_does_not_redo_completed_items(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 4)
    cfg = _cfg(workers=2)
    scripted = {f"c{i}": _clf(0.9) for i in range(4)}

    class _CrashingClient:
        synchronous = True

        def submit_one(self, request):
            if request.custom_id == "c2":
                raise RuntimeError("simulated crash")
            spec = scripted[request.custom_id]
            return llm.BatchResult(request.custom_id, "succeeded", message=_Msg(spec))

    with pytest.raises(RuntimeError, match="simulated crash"):
        analyze.analyze(
            con,
            cfg,
            embedder=embed.FakeEmbedder(db.VEC_DIM),
            batch_client=_CrashingClient(),
            rubric_path=RUBRIC,
            labels_path=LABELS,
            proposals_dir=tmp_path / "proposals",
            wait=True,
            sleep=lambda s: None,
        )

    completed_before = con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    # With workers=2, at most 2 items can already be in flight (and thus
    # persisted) by the time the crash on c2 is discovered -- the exact count
    # depends on real thread-completion order, so assert the bound, not a
    # single value.
    assert 1 <= completed_before <= 2

    # "Restart": a fresh, non-crashing client picks up wherever the crash
    # left off. No lingering `batches` row survives a parallel crash (the
    # crashing item's row is never inserted, and other items are inserted
    # and collected atomically before the exception unwinds), so this call
    # starts a fresh run and relies on clf_hash/get_classification caching
    # in _issues_to_classify to avoid redoing completed work.
    summary = analyze.analyze(
        con,
        cfg,
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=_ParallelFakeClient(scripted, delay=0.0),
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )

    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 4
    assert summary["classified"] == 4 - completed_before


def test_orphaned_synchronous_batch_is_requeued_after_restart(tmp_path):
    # Reproduces #25: a `submitted` batch row survives a crash, but the
    # synchronous claude_cli backend kept its results only in the dead
    # process's memory. A fresh client doesn't recognize the provider id, so
    # collecting it must not raise KeyError -- the batch is dropped and its
    # issue re-queued.
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 1)
    run_id = db.start_run(con, "analyze")
    batch_id = f"{run_id}:classify:0"
    db.insert_batch(con, batch_id, run_id, "classify", "cli-dead", 1)
    db.insert_batch_items(con, batch_id, {"c0": json.dumps(["r/a", 1])})
    con.commit()

    scripted = {"c0": {"status": "succeeded", "payload": _clf(0.9)}}
    client = _SyncFakeClient(scripted)  # fresh instance: empty in-memory results
    summary = analyze.analyze(
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

    # The orphaned row is gone and its issue was re-queued and classified this
    # run (the classify stage had no other batch rows to keep it "started").
    assert (
        con.execute(
            "SELECT COUNT(*) FROM batches WHERE provider_batch_id='cli-dead'"
        ).fetchone()[0]
        == 0
    )
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1
    assert summary["classified"] == 1


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


def test_rate_limited_result_halts_sequential_stage_and_requeues(tmp_path):
    # A rate limit on the 2nd classify item halts the stage: the 1st item is
    # classified, the 3rd is never submitted, and the halt is reported without
    # writing proposals (the run is left to resume next invocation).
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 3)
    scripted = {
        "c0": {"status": "succeeded", "payload": _clf(0.9)},
        "c1": {"status": "rate_limited"},
        "c2": {"status": "succeeded", "payload": _clf(0.9)},
    }
    client = _SyncFakeClient(scripted)
    summary = analyze.analyze(
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
    assert summary["halted_on_rate_limit"] is True
    assert summary["classified"] == 1
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1
    # finalization skipped -> no proposals dir created
    assert not (tmp_path / "proposals").exists()


class _RateLimitedParallelClient:
    """submit_one-only client: one custom_id returns rate_limited (instantly),
    every other item succeeds after a small delay so the rate-limited item wins
    the race and halts dispatch deterministically while an in-flight item is
    still running (proving in-flight items are drained and recorded)."""

    synchronous = True

    def __init__(self, rate_limited_cid, delay=0.1):
        self.rate_limited_cid = rate_limited_cid
        self.delay = delay
        self._lock = threading.Lock()
        self.submitted = []

    def submit_one(self, request):
        with self._lock:
            self.submitted.append(request.custom_id)
        if request.custom_id == self.rate_limited_cid:
            return llm.BatchResult(request.custom_id, "rate_limited", cost_usd=0.0)
        time.sleep(self.delay)
        return llm.BatchResult(request.custom_id, "succeeded", message=_Msg(_clf(0.9)))


def test_parallel_stage_halts_on_rate_limit_and_drains_inflight(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _n_issues(con, 4)
    client = _RateLimitedParallelClient(rate_limited_cid="c0")
    summary = analyze.analyze(
        con,
        _cfg(workers=2),
        embedder=embed.FakeEmbedder(db.VEC_DIM),
        batch_client=client,
        rubric_path=RUBRIC,
        labels_path=LABELS,
        proposals_dir=tmp_path / "proposals",
        wait=True,
        sleep=lambda s: None,
    )
    assert summary["halted_on_rate_limit"] is True
    # workers=2 fills {c0, c1}; c0 rate-limits instantly and halts before c2/c3
    # are ever dispatched, but the in-flight c1 still completes and is recorded.
    # Assert on the set/size, not order: the two worker threads append to
    # `submitted` under a lock, so their relative order is not deterministic.
    assert set(client.submitted) == {"c0", "c1"}
    assert len(client.submitted) == 2
    assert con.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 1
