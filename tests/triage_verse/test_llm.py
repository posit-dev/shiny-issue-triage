import json

from triage_verse import llm


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, payload, usage=None):
        self.content = [_Block(json.dumps(payload))]
        self.usage = usage


def test_output_config_wraps_schema():
    oc = llm.output_config_for({"type": "object"})
    assert oc == {"format": {"type": "json_schema", "schema": {"type": "object"}}}


def test_extract_json_reads_first_text_block():
    assert llm.extract_json(_Msg({"verdict": "duplicate"})) == {"verdict": "duplicate"}


def test_fake_batch_client_roundtrip():
    fake = llm.FakeBatchClient(
        scripted={
            "c0": {"status": "succeeded", "payload": {"type": "fix"}},
            "c1": {"status": "errored", "error": "invalid_request"},
        }
    )
    pid = fake.submit(
        [
            llm.BatchRequest("c0", {"model": "claude-haiku-4-5"}),
            llm.BatchRequest("c1", {"model": "claude-haiku-4-5"}),
        ]
    )
    assert fake.status(pid) == "ended"
    results = {r.custom_id: r for r in fake.results(pid)}
    assert results["c0"].status == "succeeded"
    assert llm.extract_json(results["c0"].message) == {"type": "fix"}
    assert results["c1"].status == "errored"
