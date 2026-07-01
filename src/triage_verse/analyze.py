"""The resumable analysis state machine: embed -> candidates -> batches -> proposals."""

from __future__ import annotations

import json
import time

from . import candidates, classify, db, dedup, prompts, proposals, spend


def _system_for(con, repo, rubric_path, labels_path):
    return prompts.build_system(rubric_path, labels_path, f"repo: {repo}")


def analyze(
    con,
    cfg,
    *,
    repo=None,
    limit=None,
    full=False,
    wait=False,
    embedder,
    batch_client,
    rubric_path,
    labels_path,
    proposals_dir,
    sleep=time.sleep,
    log=print,
) -> dict:
    open_now = db.open_batches(con)
    run_id = open_now[0]["run_id"] if open_now else db.start_run(con, "analyze")
    allowed = prompts.allowed_labels(labels_path)
    summary = {"classified": 0, "rechecked": 0, "pairs": 0, "halted_on_budget": False}

    # Stage 0: embed (local).
    repos = (
        [repo]
        if repo
        else [
            r["repo"]
            for r in con.execute("SELECT DISTINCT repo FROM issues").fetchall()
        ]
    )
    from . import embed as embed_mod

    embedded = 0
    for r in repos:
        embedded += embed_mod.embed_repo(con, r, embedder, full=full)
    log(f"embedded {embedded} issue(s) across {len(repos)} repo(s)")

    # Stage 1: candidate pairs (local), cached by run for recheck/proposals.
    pairs = candidates.candidate_pairs(con, cfg, repo=repo, limit=limit)
    log(f"found {len(pairs)} duplicate-candidate pair(s)")

    # Open issues needing classification.
    issues = _issues_to_classify(con, repo, limit)
    log(f"{len(issues)} issue(s) need classification")

    # --- Wave 1: classify + dedup (only if not already submitted for this run) ---
    if not _stage_started(con, run_id, "classify"):
        if (
            _submit_stage(
                con,
                cfg,
                run_id,
                "classify",
                batch_client,
                classify.build_requests(
                    con,
                    cfg,
                    cfg.classify,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    issues,
                    prefix="c",
                ),
                targets=[json.dumps([i["repo"], i["number"]]) for i in issues],
                log=log,
            )
            is False
        ):
            summary["halted_on_budget"] = True
    if not summary["halted_on_budget"] and not _stage_started(con, run_id, "dedup"):
        if (
            _submit_stage(
                con,
                cfg,
                run_id,
                "dedup",
                batch_client,
                dedup.build_requests(
                    con,
                    cfg.dedup,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    pairs,
                    prefix="d",
                ),
                targets=[
                    json.dumps([[a[0], a[1], a[2]], [b[0], b[1], b[2]]])
                    for a, b in pairs
                ],
                log=log,
            )
            is False
        ):
            summary["halted_on_budget"] = True

    _collect(con, cfg, run_id, batch_client, allowed, summary, issues, pairs, log)

    # --- Wave 2: recheck (after every classify batch collected) ---
    def maybe_recheck():
        if summary["halted_on_budget"]:
            return
        if not _stage_collected(con, run_id, "classify"):
            return
        if _stage_started(con, run_id, "recheck"):
            return
        to_recheck = _issues_needing_recheck(con, cfg, issues)
        if not to_recheck:
            return
        if (
            _submit_stage(
                con,
                cfg,
                run_id,
                "recheck",
                batch_client,
                classify.build_requests(
                    con,
                    cfg,
                    cfg.recheck,
                    _system_for(con, repo or "all", rubric_path, labels_path),
                    to_recheck,
                    prefix="r",
                    with_comments=True,
                ),
                targets=[json.dumps([i["repo"], i["number"]]) for i in to_recheck],
                log=log,
            )
            is False
        ):
            summary["halted_on_budget"] = True

    maybe_recheck()
    _collect(con, cfg, run_id, batch_client, allowed, summary, issues, pairs, log)

    if wait:
        while db.open_batches(con):
            sleep(cfg.poll_interval_seconds)
            _collect(
                con, cfg, run_id, batch_client, allowed, summary, issues, pairs, log
            )
            maybe_recheck()
            _collect(
                con, cfg, run_id, batch_client, allowed, summary, issues, pairs, log
            )

    if not db.open_batches(con) and not summary["halted_on_budget"]:
        records = proposals.build(con, run_id)
        if records:
            proposals.write(records, proposals_dir)
        db.finish_run(con, run_id, summary)
        log(f"wrote {len(records)} proposal(s) to {proposals_dir}")
    return summary


def _issues_to_classify(con, repo, limit):
    where = "WHERE is_pr=0 AND state='OPEN'" + (" AND repo=:repo" if repo else "")
    rows = con.execute(
        f"SELECT repo, number, title, body FROM issues {where} ORDER BY repo, number",
        {"repo": repo} if repo else {},
    ).fetchall()
    pending = []
    for r in rows:
        comments = classify._recent_comments(con, r["repo"], r["number"])
        h = classify.clf_hash(r["title"], r["body"], comments)
        existing = db.get_classification(con, r["repo"], r["number"])
        if existing is None or existing["clf_hash"] != h:
            pending.append(dict(r))
    return pending[:limit] if limit is not None else pending


def _issues_needing_recheck(con, cfg, issues):
    out = []
    for r in issues:
        row = db.get_classification(con, r["repo"], r["number"])
        if row is None:
            continue
        data = {
            "confidence": row["confidence"],
            "close_candidate": json.loads(row["close_candidate_json"])
            if row["close_candidate_json"]
            else None,
        }
        if row["model"] == cfg.classify.model and classify.needs_recheck(
            data, cfg.recheck.confidence_floor
        ):
            out.append(r)
    return out


def _stage_started(con, run_id, stage) -> bool:
    return any(b["stage"] == stage for b in db.run_batches(con, run_id))


def _stage_collected(con, run_id, stage) -> bool:
    rows = [b for b in db.run_batches(con, run_id) if b["stage"] == stage]
    return bool(rows) and all(b["status"] == "collected" for b in rows)


def _submit_stage(con, cfg, run_id, stage, client, requests, targets, log):
    if not requests:
        return True
    log(f"submitting {stage}: {len(requests)} item(s)")
    for start in range(0, len(requests), cfg.max_requests_per_batch):
        if spend.breaker_tripped(con, cfg):
            log(f"budget reached; not submitting more {stage} batches")
            return False
        chunk = requests[start : start + cfg.max_requests_per_batch]
        chunk_targets = targets[start : start + cfg.max_requests_per_batch]
        provider_id = client.submit(chunk)
        batch_id = f"{run_id}:{stage}:{start}"
        db.insert_batch(con, batch_id, run_id, stage, provider_id, len(chunk))
        db.insert_batch_items(
            con, batch_id, {r.custom_id: t for r, t in zip(chunk, chunk_targets)}
        )
        con.commit()
    return True


def _collect(con, cfg, run_id, client, allowed, summary, issues, pairs, log):
    for batch in db.open_batches(con):
        if client.status(batch["provider_batch_id"]) != "ended":
            continue
        items = db.get_batch_items(con, batch["batch_id"])
        count = 0
        for result in client.results(batch["provider_batch_id"]):
            target = json.loads(items[result.custom_id])
            if result.usage is not None:
                spend.record_spend(
                    con,
                    run_id,
                    batch["stage"],
                    _model(cfg, batch["stage"]),
                    cfg.pricing,
                    result.usage,
                    cost_usd=result.cost_usd,
                )
            _apply_result(
                con, cfg, run_id, batch["stage"], result, target, allowed, summary
            )
            count += 1
        db.set_batch(con, batch["batch_id"], status="collected", ended_at=db._now())
        con.commit()
        log(f"collected {batch['stage']}: {count} result(s)")


def _model(cfg, stage):
    return {
        "classify": cfg.classify.model,
        "recheck": cfg.recheck.model,
        "dedup": cfg.dedup.model,
    }[stage]


def _apply_result(con, cfg, run_id, stage, result, target, allowed, summary):
    if stage in ("classify", "recheck"):
        data = classify.parse(result)
        if data is None:
            return
        repo, number = target
        comments = classify._recent_comments(con, repo, number)
        row = con.execute(
            "SELECT title, body FROM issues WHERE repo=? AND number=?", (repo, number)
        ).fetchone()
        h = classify.clf_hash(row["title"], row["body"], comments)
        classify.store(con, repo, number, h, data, _model(cfg, stage), run_id, allowed)
        if stage == "classify":
            summary["classified"] += 1
        else:
            summary["rechecked"] += 1
    else:  # dedup
        data = dedup.parse(result)
        if data is None:
            return
        a, b = target
        dedup.store(con, (tuple(a), tuple(b)), data, _model(cfg, stage), run_id)
        summary["pairs"] += 1


def analyze_status(con) -> dict:
    return {
        "open_batches": [dict(b) for b in db.open_batches(con)],
        "today_spend_usd": db.today_spend_usd(con),
    }
