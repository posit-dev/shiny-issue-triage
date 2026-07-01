"""Per-result spend metering and the daily circuit breaker."""

from __future__ import annotations

from . import db


def usd_for_usage(
    pricing, model, *, input_tokens, cached_tokens, output_tokens
) -> float:
    rates = pricing[model]
    return (
        input_tokens / 1_000_000 * rates["input"]
        + cached_tokens / 1_000_000 * rates["cached"]
        + output_tokens / 1_000_000 * rates["output"]
    )


def record_spend(con, run_id, stage, model, pricing, usage, cost_usd=None) -> float:
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    cached_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    if cost_usd is None:
        cost_usd = usd_for_usage(
            pricing,
            model,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
        )
    db.insert_spend(
        con, run_id, stage, model, input_tokens, cached_tokens, output_tokens, cost_usd
    )
    return cost_usd


def breaker_tripped(con, cfg) -> bool:
    return db.today_spend_usd(con) >= cfg.max_usd_per_day
