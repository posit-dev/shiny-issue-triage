"""Batch model access: interface, deterministic fake, Anthropic Batch API impl."""

from __future__ import annotations

import json
import subprocess
import types
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, cast

import jsonschema

if TYPE_CHECKING:
    import anthropic
    from anthropic.types.messages.batch_create_params import Request as _BatchRequest
    from anthropic.types.messages.message_batch_succeeded_result import (
        MessageBatchSucceededResult as _SucceededResult,
    )

    from triage_verse import config


@dataclass
class BatchRequest:
    custom_id: str
    params: dict


@dataclass
class BatchResult:
    custom_id: str
    status: str  # succeeded | errored | canceled | expired
    message: Any = None  # provider message object on success
    error: Any = None
    cost_usd: float | None = None

    @property
    def usage(self):
        return getattr(self.message, "usage", None) if self.message else None


def output_config_for(schema: dict) -> dict:
    return {"format": {"type": "json_schema", "schema": schema}}


def extract_json(message) -> dict:
    text = next(b.text for b in message.content if b.type == "text")
    return json.loads(text)


class BatchClient(Protocol):
    def submit(self, requests: list[BatchRequest]) -> str: ...
    def status(self, provider_id: str) -> str: ...
    def results(self, provider_id: str) -> list[BatchResult]: ...


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    def __init__(self, payload, usage):
        self.content = [_FakeBlock(json.dumps(payload))]
        self.usage = usage


class _FakeUsage:
    def __init__(self, i=10, c=0, o=5):
        self.input_tokens, self.cache_read_input_tokens, self.output_tokens = i, c, o


class FakeBatchClient:
    """In-memory batch client. `scripted` maps custom_id -> result spec."""

    def __init__(self, scripted: dict[str, dict]):
        self.scripted = scripted
        self._batches: dict[str, list[str]] = {}

    def submit(self, requests: list[BatchRequest]) -> str:
        pid = "fake-" + uuid.uuid4().hex[:8]
        self._batches[pid] = [r.custom_id for r in requests]
        return pid

    def status(self, provider_id: str) -> str:
        return "ended"

    def recognizes(self, provider_id: str) -> bool:
        return provider_id in self._batches

    def results(self, provider_id: str) -> list[BatchResult]:
        out = []
        for cid in self._batches[provider_id]:
            spec = self.scripted.get(cid, {"status": "succeeded", "payload": {}})
            if spec["status"] == "succeeded":
                msg = _FakeMessage(spec["payload"], spec.get("usage") or _FakeUsage())
                out.append(BatchResult(cid, "succeeded", message=msg))
            else:
                out.append(BatchResult(cid, spec["status"], error=spec.get("error")))
        return out


class AnthropicBatchClient:
    """Real Anthropic Message Batches API. Reads ANTHROPIC_API_KEY from the env."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def submit(self, requests: list[BatchRequest]) -> str:
        batch = self._client.messages.batches.create(
            requests=cast(
                "list[_BatchRequest]",
                [{"custom_id": r.custom_id, "params": r.params} for r in requests],
            )
        )
        return batch.id

    def status(self, provider_id: str) -> str:
        return self._client.messages.batches.retrieve(provider_id).processing_status

    def results(self, provider_id: str) -> list[BatchResult]:
        out = []
        for r in self._client.messages.batches.results(provider_id):
            kind = r.result.type
            if kind == "succeeded":
                out.append(
                    BatchResult(
                        r.custom_id,
                        "succeeded",
                        message=cast("_SucceededResult", r.result).message,
                    )
                )
            else:
                err = getattr(r.result, "error", None)
                out.append(BatchResult(r.custom_id, kind, error=err))
        return out


# "claude-sonnet-5" maps to itself, not the short "sonnet" alias: on this CLI
# install, `--model sonnet` still resolves to the older claude-sonnet-4-6
# (verified empirically), while `--model claude-sonnet-5` correctly reaches
# the new model. Passing the literal id sidesteps the CLI's alias staleness.
_MODEL_ALIASES = {"claude-haiku-4-5": "haiku", "claude-sonnet-5": "claude-sonnet-5"}
_MAX_PROMPT_CHARS = 50_000
_CLI_TIMEOUT = 300  # seconds; a hung `claude -p` must not block the run forever.


def _default_runner(args: list[str], prompt: str) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_CLI_TIMEOUT,
    )
    # `claude -p --output-format json` prints its result envelope -- including
    # is_error / api_error_status on a rate limit -- to stdout and still exits
    # non-zero. Surface that stdout so submit_one can classify the failure from
    # the envelope; only a non-zero exit with no stdout is an opaque crash
    # (or timeout) worth raising with its stderr for diagnostics.
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"claude -p exited {proc.returncode}: {proc.stderr[:500]}")
    return proc.stdout


def _extract_json_object(text: str) -> dict:
    t = text.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j > i:
            return json.loads(t[i : j + 1])
        raise ValueError("no JSON object in output") from None


class _CliBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _CliMessage:
    def __init__(self, data: dict, usage: object) -> None:
        self.content = [_CliBlock(json.dumps(data))]
        self.usage = usage


class ClaudeCliClient:
    """Runs `claude -p` per request on Claude Code's ambient auth (no API key).

    Unlike the Anthropic Batch API client, this backend executes each request
    and incurs its real cost synchronously inside `submit()`/`submit_one()`,
    before results are ever collected. `analyze._submit_stage` detects this
    via the `synchronous` marker below. With `cfg.workers == 1` (the
    default), it chunks at size 1 and collects each item immediately after
    it is submitted, so the daily-budget breaker is checked before every
    single item. With `cfg.workers > 1`, up to that many `submit_one` calls
    run concurrently in a bounded worker pool, and the breaker is checked
    before each new dispatch -- bounding a tripped budget's overshoot, and a
    crash's loss, to at most `cfg.workers` items instead of 1. Either way,
    this is enforced *within* a stage, not just between stages/runs.
    """

    synchronous = True

    def __init__(
        self,
        runner=_default_runner,
        aliases=_MODEL_ALIASES,
        log: Callable[[str], None] = lambda msg: None,
    ) -> None:
        self._runner = runner
        self._aliases = aliases
        self._log = log
        self._batches: dict[str, list[BatchResult]] = {}

    def submit(self, requests: list[BatchRequest]) -> str:
        pid = "cli-" + uuid.uuid4().hex[:8]
        total = len(requests)
        results = []
        for i, r in enumerate(requests, start=1):
            result = self.submit_one(r)
            self._log(
                f"  [{i}/{total}] {result.custom_id}: {result.status}"
                f" (${result.cost_usd or 0:.4f})"
            )
            results.append(result)
        self._batches[pid] = results
        return pid

    def status(self, provider_id: str) -> str:
        return "ended"

    def recognizes(self, provider_id: str) -> bool:
        """Whether this instance holds results for provider_id.

        This backend keeps results only in memory (`self._batches`), so a
        freshly constructed instance -- e.g. after a process restart --
        recognizes none of the provider ids a prior process submitted. A
        `submitted` batch row that survives a crash refers to results this
        instance never produced; `analyze` uses this to drop such orphaned
        batches and re-queue their items rather than raising KeyError in
        `results()` (see #25).
        """
        return provider_id in self._batches

    def results(self, provider_id: str) -> list[BatchResult]:
        return self._batches[provider_id]

    def submit_one(self, request: BatchRequest) -> BatchResult:
        params = request.params
        model = self._aliases.get(params["model"], params["model"])
        schema = params["output_config"]["format"]["schema"]
        system = "\n".join(b["text"] for b in params["system"])
        # Assumes the single-user-message shape that classify.build_requests /
        # dedup.build_requests produce (one user message, no multi-turn history).
        user = str(params["messages"][0]["content"])[:_MAX_PROMPT_CHARS]
        total_cost = 0.0
        last_usage: object = types.SimpleNamespace(
            input_tokens=0, cache_read_input_tokens=0, output_tokens=0
        )
        last_error: object = "cli-output-invalid"
        for attempt in range(2):
            nudge = (
                ""
                if attempt == 0
                else "\nReturn ONLY the JSON object, with no prose and no code fences."
            )
            sys_prompt = (
                system
                + "\n\nRespond ONLY with a JSON object matching this schema:\n"
                + json.dumps(schema)
                + nudge
            )
            args = [
                "--model",
                model,
                "--output-format",
                "json",
                "--system-prompt",
                sys_prompt,
                "--tools",
                "",
            ]
            try:
                envelope = json.loads(self._runner(args, user))
            except Exception as exc:  # noqa: BLE001 - any runner/parse failure is a failed attempt
                last_error = f"cli-call-failed: {exc}"
                continue
            total_cost += float(envelope.get("total_cost_usd") or 0.0)
            last_usage = _usage_ns(envelope.get("usage") or {})
            try:
                data = _extract_json_object(envelope["result"])
                jsonschema.validate(data, schema)
            except (ValueError, json.JSONDecodeError, jsonschema.ValidationError):
                last_error = "cli-output-invalid"
                continue
            return BatchResult(
                request.custom_id,
                "succeeded",
                message=_CliMessage(data, last_usage),
                cost_usd=total_cost,
            )
        return BatchResult(
            request.custom_id,
            "errored",
            error=last_error,
            cost_usd=total_cost,
        )


def _usage_ns(usage: dict) -> object:
    return types.SimpleNamespace(
        input_tokens=usage.get("input_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )


def make_batch_client(
    cfg: config.ModelsConfig, *, log: Callable[[str], None] = print
) -> BatchClient:
    if cfg.backend == "anthropic_batch":
        return AnthropicBatchClient()
    return ClaudeCliClient(log=log)
