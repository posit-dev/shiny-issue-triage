"""Tests for embed/analyze/analyze-status CLI subcommands."""

from __future__ import annotations

from triage_verse import analyze as analyze_mod
from triage_verse import cli
from triage_verse import embed as embed_mod
from triage_verse import llm


def test_cli_analyze_invokes_pipeline(tmp_path, monkeypatch):
    captured = {}

    def fake_analyze(con, cfg, **kw):
        captured.update(kw)
        captured["cfg"] = cfg
        return {"classified": 3, "rechecked": 1, "pairs": 2, "halted_on_budget": False}

    monkeypatch.setattr(analyze_mod, "analyze", fake_analyze)
    # Prevent real FastEmbedEmbedder (lazy-imports fastembed + downloads model)
    monkeypatch.setattr(embed_mod, "FastEmbedEmbedder", lambda *a, **k: object())
    # Prevent real AnthropicBatchClient (needs Anthropic SDK + credentials)
    monkeypatch.setattr(llm, "AnthropicBatchClient", lambda *a, **k: object())

    rc = cli.main(
        [
            "analyze",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--models-config",
            str(_models_yaml(tmp_path)),
            "--repo",
            "rstudio/shinytest2",
            "--limit",
            "5",
            "--wait",
            "--proposals-dir",
            str(tmp_path / "proposals"),
        ]
    )
    assert rc == 0
    assert captured["repo"] == "rstudio/shinytest2"
    assert captured["limit"] == 5
    assert captured["wait"] is True


def _models_yaml(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(
        "embedding: {model: m, dim: 384, candidate_top_k: 10, cosine_threshold: 0.8}\n"
        "stages:\n  classify: {model: claude-haiku-4-5, max_tokens: 512}\n"
        "  recheck: {model: claude-sonnet-5, max_tokens: 1024, confidence_floor: 0.7}\n"
        "  dedup: {model: claude-sonnet-5, max_tokens: 1024}\n"
        "batch: {max_requests_per_batch: 500, poll_interval_seconds: 30}\n"
        "spend: {batch_only: true, max_usd_per_day: 50, pricing: {}}\n"
    )
    return p


def test_cli_embed_invokes_embed_repo(tmp_path, monkeypatch):
    from triage_verse import embed as embed_mod

    cfg_repos = tmp_path / "repos.yaml"
    cfg_repos.write_text("repositories:\n  - rstudio/shinytest2\n")
    monkeypatch.setattr(embed_mod, "FastEmbedEmbedder", lambda *a, **k: object())
    calls = []
    monkeypatch.setattr(
        embed_mod,
        "embed_repo",
        lambda con, repo, embedder, **kw: calls.append(repo) or 3,
    )
    rc = cli.main(
        [
            "embed",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--config",
            str(cfg_repos),
            "--models-config",
            str(_models_yaml(tmp_path)),
        ]
    )
    assert rc == 0
    assert calls == ["rstudio/shinytest2"]


def test_cli_analyze_uses_backend_factory(tmp_path, monkeypatch):
    made = {}
    monkeypatch.setattr(embed_mod, "FastEmbedEmbedder", lambda *a, **k: object())
    monkeypatch.setattr(
        llm,
        "make_batch_client",
        lambda cfg, **kw: made.setdefault("client", object()),
    )
    monkeypatch.setattr(
        analyze_mod,
        "analyze",
        lambda con, cfg, **kw: {
            "classified": 0,
            "rechecked": 0,
            "pairs": 0,
            "halted_on_budget": False,
        },
    )
    rc = cli.main(
        [
            "analyze",
            "--db",
            str(tmp_path / "m.sqlite"),
            "--models-config",
            str(_models_yaml(tmp_path)),
        ]
    )
    assert rc == 0 and "client" in made


def test_cli_analyze_status_runs(tmp_path):
    from triage_verse import db

    db.connect(tmp_path / "m.sqlite").close()
    rc = cli.main(["analyze-status", "--db", str(tmp_path / "m.sqlite")])
    assert rc == 0
