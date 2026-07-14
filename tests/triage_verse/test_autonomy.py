"""Graduated-autonomy precision, promotion, demotion."""

from triage_verse import autonomy, config


CFG = config.AutonomyConfig(min_decisions=4, min_precision=0.75,
                            confidence_floor=0.9, audit_rate=0.10)


def _d(action, verdict):
    return {"action": action, "verdict": verdict}


def test_category_precision_counts_success_and_failure():
    decisions = [_d("add-label", "approved"), _d("add-label", "approved"),
                 _d("add-label", "rejected"), _d("add-label", "skipped")]
    prec = autonomy.category_precision(decisions)
    assert prec["add-label"]["reviewed"] == 3  # skipped excluded
    assert abs(prec["add-label"]["precision"] - 2 / 3) < 1e-9


def test_promote_only_when_thresholds_met():
    good = [_d("add-label", "approved")] * 4
    bad = [_d("set-priority", "approved")] * 2 + [_d("set-priority", "rejected")] * 2
    ev = autonomy.evaluate(good + bad, [], CFG)
    assert ev["add-label"]["promote"] is True
    assert ev["set-priority"]["promote"] is False  # 0.5 < 0.75


def test_close_never_eligible():
    decisions = [_d("close", "approved")] * 100
    ev = autonomy.evaluate(decisions, [], CFG)
    assert "close" not in ev


def test_audit_rejection_demotes_via_precision():
    decisions = [_d("add-label", "approved")] * 4
    # one audit rejection recorded in results counts as a failure
    results = [{"action": "add-label", "audit_verdict": "rejected"}]
    ev = autonomy.evaluate(decisions, results, CFG)
    # 4 success + 1 failure = 0.8 >= 0.75 still promotes; add a second failure:
    results += [{"action": "add-label", "audit_verdict": "rejected"}]
    ev2 = autonomy.evaluate(decisions, results, CFG)
    assert ev["add-label"]["promote"] is True
    assert ev2["add-label"]["promote"] is False  # 4/6 = 0.667 < 0.75


def test_render_config_lists_promoted_only():
    good = [_d("add-label", "approved")] * 4
    ev = autonomy.evaluate(good, [], CFG)
    doc = autonomy.render_config(ev, CFG, today="2026-08-01")
    assert doc == {"promoted": {"add-label": {"promoted_at": "2026-08-01",
                                              "confidence_floor": 0.9}}}
