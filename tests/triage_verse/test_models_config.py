# tests/triage_verse/test_models_config.py
import pathlib

from triage_verse.config import ModelsConfig, load_models_config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_load_models_config_parses_checked_in_file():
    cfg = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert isinstance(cfg, ModelsConfig)
    assert cfg.embed_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert cfg.embed_dim == 384
    assert cfg.cosine_threshold == 0.80
    assert cfg.classify.model == "claude-haiku-4-5"
    assert cfg.recheck.confidence_floor == 0.70
    assert cfg.dedup.model == "claude-sonnet-5"
    assert cfg.max_requests_per_batch == 500
    assert cfg.batch_only is True
    assert cfg.max_usd_per_day == 50
    assert cfg.pricing["claude-haiku-4-5"]["output"] == 2.50


def test_load_models_config_from_string(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "embedding: {model: m, dim: 8, candidate_top_k: 3, cosine_threshold: 0.5}\n"
        "stages:\n"
        "  classify: {model: claude-haiku-4-5, max_tokens: 100}\n"
        "  recheck: {model: claude-sonnet-5, max_tokens: 200, confidence_floor: 0.6}\n"
        "  dedup: {model: claude-sonnet-5, max_tokens: 200}\n"
        "batch: {max_requests_per_batch: 50, poll_interval_seconds: 5}\n"
        "spend: {batch_only: true, max_usd_per_day: 1, pricing: {claude-haiku-4-5: {input: 1, cached: 0.1, output: 2}}}\n"
    )
    cfg = load_models_config(p)
    assert cfg.embed_dim == 8
    assert cfg.classify.confidence_floor is None


def test_backend_defaults_to_claude_cli(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "embedding: {model: m, dim: 8, candidate_top_k: 3, cosine_threshold: 0.5}\n"
        "stages:\n"
        "  classify: {model: claude-haiku-4-5, max_tokens: 100}\n"
        "  recheck: {model: claude-sonnet-5, max_tokens: 200, confidence_floor: 0.6}\n"
        "  dedup: {model: claude-sonnet-5, max_tokens: 200}\n"
        "batch: {max_requests_per_batch: 50, poll_interval_seconds: 5}\n"
        "spend: {batch_only: true, max_usd_per_day: 1, pricing: {}}\n"
    )
    assert load_models_config(p).backend == "claude_cli"


def test_backend_read_from_file():
    cfg = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert cfg.backend in {"claude_cli", "anthropic_batch"}


def test_workers_defaults_to_1_when_absent(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "embedding: {model: m, dim: 8, candidate_top_k: 3, cosine_threshold: 0.5}\n"
        "stages:\n"
        "  classify: {model: claude-haiku-4-5, max_tokens: 100}\n"
        "  recheck: {model: claude-sonnet-5, max_tokens: 200, confidence_floor: 0.6}\n"
        "  dedup: {model: claude-sonnet-5, max_tokens: 200}\n"
        "batch: {max_requests_per_batch: 50, poll_interval_seconds: 5}\n"
        "spend: {batch_only: true, max_usd_per_day: 1, pricing: {}}\n"
    )
    assert load_models_config(p).workers == 1


def test_workers_read_from_checked_in_config():
    cfg = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert cfg.workers == 2


def test_tiers_and_autonomy_defaults_and_overrides(tmp_path):
    from triage_verse import config

    base = (
        "backend: claude_cli\n"
        "embedding: {model: m, dim: 3, candidate_top_k: 1, cosine_threshold: 0.8}\n"
        "stages:\n"
        "  classify: {model: c, max_tokens: 1}\n"
        "  recheck: {model: r, max_tokens: 1}\n"
        "  dedup: {model: d, max_tokens: 1}\n"
        "batch: {max_requests_per_batch: 1, poll_interval_seconds: 1}\n"
        "spend: {batch_only: true, max_usd_per_day: 1, pricing: {}}\n"
    )
    p = tmp_path / "m.yaml"
    p.write_text(base, encoding="utf-8")
    cfg = config.load_models_config(p)
    assert cfg.tiers.tier1_max_per_day == 25
    assert cfg.tiers.tier2_max_per_week == 10
    assert cfg.autonomy.min_decisions == 200
    assert cfg.autonomy.min_precision == 0.98
    assert cfg.autonomy.confidence_floor == 0.9
    assert cfg.autonomy.audit_rate == 0.10

    p.write_text(
        base + "tiers: {tier1_max_per_day: 5, tier2_max_per_week: 2}\n"
        "autonomy: {min_decisions: 50, min_precision: 0.95,"
        " confidence_floor: 0.8, audit_rate: 0.25}\n",
        encoding="utf-8",
    )
    cfg = config.load_models_config(p)
    assert cfg.tiers.tier1_max_per_day == 5
    assert cfg.autonomy.min_decisions == 50
    assert cfg.autonomy.audit_rate == 0.25
