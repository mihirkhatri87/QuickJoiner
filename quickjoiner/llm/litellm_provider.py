"""LiteLLM provider — speaks the OpenAI Chat Completions wire format over HTTP.

LiteLLM's proxy server exposes an OpenAI-compatible `/chat/completions` endpoint
that fronts 100+ model backends (OpenAI, Anthropic, Azure, Bedrock, vLLM, local…),
so pointing this provider at a LiteLLM proxy `base_url` unlocks any of them through
one config. It also works with any other OpenAI-compatible endpoint.

Supports SSE token streaming (on_stream), tool calling, and best-effort reasoning
passthrough (OpenAI-compatible endpoints that emit `reasoning_content` — e.g. via
LiteLLM's normalization — surface it on the "thinking" stream).

The wire translation lives in pure module-level functions (`_to_wire`,
`accumulate_delta`, `_tool_calls_from_*`) so it is unit-tested without a live
server; `httpx` I/O is done through an injectable transport for MockTransport tests.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from quickjoiner.config import LLMConfig
from quickjoiner.llm.base import ChatResult, LLMProvider, Message, StreamCallback, ToolCall, ToolSpec


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """OpenAI tool-call arguments arrive as a JSON string (or already a dict in some
    proxies). Parse leniently — a malformed/partial payload yields an empty dict rather
    than crashing the tool loop."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_calls_from_message(message: dict[str, Any]) -> list[ToolCall]:
    """Build ToolCalls from a non-streamed assistant message."""
    calls: list[ToolCall] = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        calls.append(
            ToolCall(id=tc.get("id") or f"call_{i}", name=name, input=_parse_arguments(fn.get("arguments")))
        )
    return calls


def _tool_calls_from_acc(slots: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """Build ToolCalls from streamed deltas accumulated by index."""
    calls: list[ToolCall] = []
    for idx, slot in sorted(slots.items()):
        if not slot.get("name"):
            continue
        calls.append(
            ToolCall(id=slot.get("id") or f"call_{idx}", name=slot["name"], input=_parse_arguments(slot.get("arguments")))
        )
    return calls


def accumulate_delta(delta: dict[str, Any], acc: dict[str, Any], on_stream: StreamCallback | None) -> None:
    """Fold one streamed choice `delta` into the accumulator (pure; testable).

    Tool calls stream as fragments keyed by `index`: the id/name land in the first
    fragment and `arguments` is concatenated across fragments, so we merge by index.
    """
    content = delta.get("content")
    if content:
        acc["text"] += content
        if on_stream:
            on_stream("text", content)
    reasoning = delta.get("reasoning_content")
    if reasoning:
        acc["thinking"] += reasoning
        if on_stream:
            on_stream("thinking", reasoning)
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        slot = acc["tool_calls"].setdefault(idx, {"id": None, "name": None, "arguments": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]


def _sse_json(line: str) -> dict[str, Any] | None:
    """Decode one Server-Sent-Events line into JSON, skipping keep-alives and the
    terminal `data: [DONE]` sentinel."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


class LiteLLMProvider(LLMProvider):
    name = "litellm"

    def __init__(self, config: LLMConfig, transport: httpx.BaseTransport | None = None):
        base = config.base_url.rstrip("/")
        # Accept a bare proxy URL, a `/v1` base, or a full endpoint interchangeably.
        self._url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self._model = config.resolved_model()
        self._max_tokens = config.max_tokens
        key = os.environ.get(config.api_key_env) if config.api_key_env else None
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._transport = transport

    def _client(self) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(transport=self._transport, timeout=300.0)
        return httpx.Client(timeout=300.0)

    def chat(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_wire(messages, system),
            "max_tokens": self._max_tokens,
            "stream": on_stream is not None,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        if on_stream is not None:
            return self._chat_stream(payload, on_stream)
        return self._chat_once(payload)

    def _chat_once(self, payload: dict[str, Any]) -> ChatResult:
        with self._client() as client:
            resp = client.post(self._url, json=payload, headers=self._headers)
            resp.raise_for_status()
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return ChatResult(
            text=message.get("content") or "",
            tool_calls=_tool_calls_from_message(message),
            thinking=message.get("reasoning_content") or "",
            stop_reason=choice.get("finish_reason"),
        )

    def _chat_stream(self, payload: dict[str, Any], on_stream: StreamCallback) -> ChatResult:
        acc: dict[str, Any] = {"text": "", "thinking": "", "tool_calls": {}}
        finish: str | None = None
        with self._client() as client:
            with client.stream("POST", self._url, json=payload, headers=self._headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    data = _sse_json(line)
                    if data is None:
                        continue
                    choice = (data.get("choices") or [{}])[0]
                    accumulate_delta(choice.get("delta") or {}, acc, on_stream)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        return ChatResult(
            text=acc["text"],
            tool_calls=_tool_calls_from_acc(acc["tool_calls"]),
            thinking=acc["thinking"],
            stop_reason=finish,
        )

    @staticmethod
    def _to_wire(messages: list[Message], system: str | None) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        if system:
            wire.append({"role": "system", "content": system})
        for m in messages:
            role = m["role"]
            if role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                        }
                        for tc in m["tool_calls"]
                    ]
                    # OpenAI permits (and prefers) null content alongside tool calls.
                    if not entry["content"]:
                        entry["content"] = None
                wire.append(entry)
            elif role == "tool":
                # tool_call_id must match an id from the preceding assistant turn.
                wire.append(
                    {"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": m["content"]}
                )
            else:
                wire.append({"role": "user", "content": m["content"]})
        return wire
