import json

from triage_verse import llm

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["duplicate", "distinct"]}},
    "required": ["verdict"],
    "additionalProperties": False,
}


def _request(cid="c0", model="claude-haiku-4-5"):
    return llm.BatchRequest(
        cid,
        {
            "model": model,
            "system": [
                {
                    "type": "text",
                    "text": "RUBRIC",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {"role": "user", "content": "<ISSUE_TITLE>\nx\n</ISSUE_TITLE>"}
            ],
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
        },
    )


def _envelope(result_text, cost=0.01):
    return json.dumps(
        {
            "type": "result",
            "result": result_text,
            "total_cost_usd": cost,
            "usage": {
                "input_tokens": 5,
                "output_tokens": 10,
                "cache_read_input_tokens": 0,
            },
        }
    )


def test_parses_fenced_json_and_maps_model():
    calls = []

    def runner(args, prompt):
        calls.append((args, prompt))
        return _envelope('```json\n{"verdict": "duplicate"}\n```', cost=0.02)

    client = llm.ClaudeCliClient(runner=runner)
    pid = client.submit([_request()])
    assert client.status(pid) == "ended"
    (result,) = client.results(pid)
    assert result.status == "succeeded"
    assert result.cost_usd == 0.02
    assert llm.extract_json(result.message) == {"verdict": "duplicate"}
    # command disables tools (last) and selects the haiku alias, json output
    args = calls[0][0]
    assert args[-2:] == ["--tools", ""]
    assert "--output-format" in args and "json" in args
    assert "haiku" in args


def test_retries_once_on_schema_violation_then_succeeds():
    envs = iter(
        [_envelope('{"verdict": "MAYBE"}'), _envelope('{"verdict": "distinct"}')]
    )

    def runner(args, prompt):
        return next(envs)

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "succeeded"
    assert llm.extract_json(result.message) == {"verdict": "distinct"}


def test_errored_after_two_bad_outputs_and_sums_cost():
    def runner(args, prompt):
        return _envelope("not json at all", cost=0.03)

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "errored"
    assert result.cost_usd == 0.06  # both attempts metered


def test_submit_one_errors_without_raising_when_runner_always_fails():
    def runner(args, prompt):
        raise RuntimeError("boom")

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "errored"


def test_submit_one_recovers_after_one_runner_failure():
    calls = {"n": 0}

    def runner(args, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return _envelope('{"verdict": "distinct"}')

    result = llm.ClaudeCliClient(runner=runner).submit_one(_request())
    assert result.status == "succeeded"
    assert llm.extract_json(result.message) == {"verdict": "distinct"}


def test_make_batch_client_selects_impl(monkeypatch):
    # AnthropicBatchClient() constructs anthropic.Anthropic(), which requires a
    # key to be present (no network call); set a dummy one so the test is offline.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert isinstance(llm.make_batch_client(_cfg("claude_cli")), llm.ClaudeCliClient)
    assert isinstance(
        llm.make_batch_client(_cfg("anthropic_batch")), llm.AnthropicBatchClient
    )


def _cfg(backend):
    from triage_verse import config

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
        50,
        {},
        backend=backend,
    )
