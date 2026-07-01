from triage_verse import config, db, spend

PRICING = {"claude-haiku-4-5": {"input": 0.50, "cached": 0.05, "output": 2.50}}


class _Usage:
    def __init__(self, i, c, o):
        self.input_tokens, self.cache_read_input_tokens, self.output_tokens = i, c, o


def test_usd_for_usage_uses_batch_rates():
    usd = spend.usd_for_usage(
        PRICING,
        "claude-haiku-4-5",
        input_tokens=1_000_000,
        cached_tokens=0,
        output_tokens=0,
    )
    assert usd == 0.50
    usd2 = spend.usd_for_usage(
        PRICING,
        "claude-haiku-4-5",
        input_tokens=0,
        cached_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert round(usd2, 4) == round(0.05 + 2.50, 4)


def test_record_spend_writes_row_and_returns_usd(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    usd = spend.record_spend(
        con, "run1", "classify", "claude-haiku-4-5", PRICING, _Usage(1_000_000, 0, 0)
    )
    assert usd == 0.50
    assert con.execute("SELECT COUNT(*) FROM spend").fetchone()[0] == 1


def _cfg(cap):
    return config.ModelsConfig(
        "m",
        8,
        10,
        0.8,
        config.StageConfig("claude-haiku-4-5", 512),
        config.StageConfig("claude-sonnet-4-6", 1024, 0.7),
        config.StageConfig("claude-sonnet-4-6", 1024),
        500,
        30,
        True,
        cap,
        PRICING,
    )


def test_record_spend_prefers_explicit_cost_usd(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    # pricing would compute 0.50, but explicit cost_usd wins
    usd = spend.record_spend(
        con,
        "run1",
        "classify",
        "claude-haiku-4-5",
        PRICING,
        _Usage(1_000_000, 0, 0),
        cost_usd=0.0188,
    )
    assert usd == 0.0188
    row = con.execute("SELECT usd, input_tokens FROM spend").fetchone()
    assert row["usd"] == 0.0188 and row["input_tokens"] == 1_000_000


def test_breaker_trips_when_daily_spend_at_cap(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert spend.breaker_tripped(con, _cfg(cap=1.0)) is False
    db.insert_spend(con, "run1", "classify", "claude-haiku-4-5", 0, 0, 0, 1.0)
    assert spend.breaker_tripped(con, _cfg(cap=1.0)) is True
