"""Ollama provider for fully local inference (native /api/chat with tool calling).

Supports NDJSON token streaming (on_stream callback) and think mode for
reasoning models (config: llm.thinking -> "think": true; the model returns a
separate "thinking" field alongside content).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from quickjoiner.config import LLMConfig
from quickjoiner.llm.base import (
    ChatResult, LLMProvider, Message, StreamCallback, TokenUsage, ToolCall, ToolSpec,
)


def accumulate_chunk(message: dict[str, Any], acc: dict[str, Any], on_stream: StreamCallback | None) -> None:
    """Fold one streamed chat chunk's message into the accumulator (pure; testable)."""
    content = message.get("content") or ""
    thinking = message.get("thinking") or ""
    if content:
        acc["text"] += content
        if on_stream:
            on_stream("text", content)
    if thinking:
        acc["thinking"] += thinking
        if on_stream:
            on_stream("thinking", thinking)
    for tc in message.get("tool_calls") or []:
        acc["tool_calls"].append(tc)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, config: LLMConfig):
        self._base_url = config.base_url.rstrip("/")
        self._model = config.resolved_model()
        self._thinking = config.thinking

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
            "stream": on_stream is not None,
        }
        if self._thinking:
            payload["think"] = True
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

        final: dict[str, Any] = {}  # the done chunk / response carries the token counts
        if on_stream is not None:
            acc: dict[str, Any] = {"text": "", "thinking": "", "tool_calls": []}
            with httpx.stream(
                "POST", f"{self._base_url}/api/chat", json=payload, timeout=300.0
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    accumulate_chunk(chunk.get("message", {}), acc, on_stream)
                    if chunk.get("done"):
                        final = chunk
                        break
            raw_calls = acc["tool_calls"]
            text, thinking = acc["text"], acc["thinking"]
        else:
            resp = httpx.post(f"{self._base_url}/api/chat", json=payload, timeout=300.0)
            resp.raise_for_status()
            final = resp.json()
            message = final.get("message", {})
            raw_calls = message.get("tool_calls") or []
            text, thinking = message.get("content", ""), message.get("thinking", "") or ""

        tool_calls = [
            ToolCall(
                id=f"call_{i}",
                name=tc["function"]["name"],
                input=tc["function"].get("arguments") or {},
            )
            for i, tc in enumerate(raw_calls)
        ]
        return ChatResult(
            text=text, tool_calls=tool_calls, thinking=thinking,
            # Ollama names these prompt_eval_count/eval_count; it reports no cache
            # counters, so `cached` stays 0 (KV-prefix reuse is invisible over /api/chat).
            usage=TokenUsage(
                prompt=int(final.get("prompt_eval_count") or 0),
                completion=int(final.get("eval_count") or 0),
            ),
        )

    @staticmethod
    def _to_wire(messages: list[Message], system: str | None) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        if system:
            wire.append({"role": "system", "content": system})
        for m in messages:
            role = m["role"]
            if role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.get("content", "")}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"function": {"name": tc.name, "arguments": tc.input}}
                        for tc in m["tool_calls"]
                    ]
                wire.append(entry)
            elif role == "tool":
                wire.append({"role": "tool", "tool_name": m.get("name", ""), "content": m["content"]})
            else:
                wire.append({"role": "user", "content": m["content"]})
        return wire
