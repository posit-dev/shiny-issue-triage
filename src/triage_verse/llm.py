"""Batch model access: interface, deterministic fake, Anthropic Batch API impl."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    import anthropic
    from anthropic.types.messages.batch_create_params import Request as _BatchRequest
    from anthropic.types.messages.message_batch_succeeded_result import (
        MessageBatchSucceededResult as _SucceededResult,
    )


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
