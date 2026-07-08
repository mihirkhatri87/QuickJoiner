"""Anthropic Claude API provider (tool calling via the Messages API).

Supports token streaming (on_stream callback) and extended thinking
(config: llm.thinking / llm.thinking_budget). Signed thinking blocks are
carried back on the next turn via the assistant message's "thinking_blocks".
"""

from __future__ import annotations

from typing import Any

from quickjoiner.config import LLMConfig
from quickjoiner.llm.base import ChatResult, LLMProvider, Message, StreamCallback, ToolCall, ToolSpec


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, config: LLMConfig):
        import anthropic  # lazy so the ollama-only path doesn't need the package configured

        self._client = anthropic.Anthropic()
        self._model = config.resolved_model()
        self._thinking = config.thinking
        self._budget = config.thinking_budget
        # The API requires max_tokens to exceed the thinking budget.
        self._max_tokens = max(config.max_tokens, self._budget + 2048) if self._thinking else config.max_tokens

    def chat(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ChatResult:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": self._to_wire(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        if self._thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self._budget}

        if on_stream is not None:
            with self._client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "content_block_delta":
                        delta = event.delta
                        if getattr(delta, "type", "") == "text_delta":
                            on_stream("text", delta.text)
                        elif getattr(delta, "type", "") == "thinking_delta":
                            on_stream("thinking", delta.thinking)
                response = stream.get_final_message()
        else:
            response = self._client.messages.create(**kwargs)

        return self._to_result(response)

    @staticmethod
    def _to_result(response: Any) -> ChatResult:
        if response.stop_reason == "refusal":
            return ChatResult(
                text="The model declined to answer this request.",
                stop_reason="refusal",
            )
        text = "".join(b.text for b in response.content if b.type == "text")
        thinking = "".join(b.thinking for b in response.content if b.type == "thinking")
        # Signed/redacted thinking blocks must be echoed back verbatim next turn.
        thinking_blocks = [
            b.model_dump() for b in response.content if b.type in ("thinking", "redacted_thinking")
        ]
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        return ChatResult(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            thinking=thinking,
            thinking_blocks=thinking_blocks,
        )

    @staticmethod
    def _to_wire(messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role == "user":
                wire.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                # Thinking blocks must come first in the assistant turn.
                blocks.extend(m.get("thinking_blocks") or [])
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append(
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                    )
                wire.append({"role": "assistant", "content": blocks or m.get("content", "")})
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                }
                # All tool results for one assistant turn must land in a single user message.
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list):
                    wire[-1]["content"].append(block)
                else:
                    wire.append({"role": "user", "content": [block]})
        return wire
