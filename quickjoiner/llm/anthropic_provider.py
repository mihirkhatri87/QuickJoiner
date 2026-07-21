"""Anthropic Claude API provider (tool calling via the Messages API).

Supports token streaming (on_stream callback), adaptive extended thinking
(config: llm.thinking), and prompt caching (config: llm.prompt_cache, on by
default). Signed thinking blocks are carried back on the next turn via the
assistant message's "thinking_blocks".

Prompt caching layout (a prefix match over tools -> system -> messages):
- one cache_control breakpoint on the system prompt caches tools+system together
  (tools render before system, so the marker covers both);
- a moving breakpoint on the last content block of the last message caches the
  growing conversation, so rounds 2..N of a tool loop and every follow-up turn
  re-read the prefix at ~0.1x input price instead of re-paying full price;
- intermediate markers every ~15 blocks walking backward keep the newest
  breakpoint within the API's 20-block cache lookback window even when a single
  tool-heavy turn emits many tool_use/tool_result blocks.
Verification: usage cache fields are logged at DEBUG — if cache_read_input_tokens
stays 0 across a tool loop, a silent invalidator is at work (e.g. the prefix is
under the model's minimum cacheable length, 4096 tokens on Opus 4.8).
"""

from __future__ import annotations

import logging
from typing import Any

from quickjoiner.config import LLMConfig
from quickjoiner.llm.base import ChatResult, LLMProvider, Message, StreamCallback, ToolCall, ToolSpec

logger = logging.getLogger(__name__)

# Keep the newest breakpoint within the API's 20-block lookback of the previous
# one; 15 leaves slack for the marked message's own blocks.
_CACHE_SPACING_BLOCKS = 15
# The API allows 4 cache_control breakpoints per request; one is spent on the
# system prompt, leaving 3 for the conversation.
_MAX_MESSAGE_MARKS = 3
# Block types cache_control may be attached to (thinking blocks may not).
_CACHEABLE_TYPES = {"text", "tool_use", "tool_result", "image", "document"}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, config: LLMConfig):
        import anthropic  # lazy so the ollama-only path doesn't need the package configured

        self._client = anthropic.Anthropic()
        self._model = config.resolved_model()
        self._thinking = config.thinking
        self._cache = config.prompt_cache
        # Thinking output counts against max_tokens, so leave headroom when it's on.
        self._max_tokens = (
            max(config.max_tokens, config.thinking_budget + 2048) if self._thinking else config.max_tokens
        )

    def chat(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ChatResult:
        kwargs = self._build_kwargs(messages, system, tools)

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

    def _build_kwargs(
        self,
        messages: list[Message],
        system: str | None,
        tools: list[ToolSpec] | None,
    ) -> dict[str, Any]:
        wire = self._to_wire(messages)
        if self._cache:
            wire = self._mark_cache_breakpoints(wire)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": wire,
        }
        if system:
            if self._cache:
                # Tools render before system, so this one breakpoint caches both.
                kwargs["system"] = [
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ]
            else:
                kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        if self._thinking:
            # Adaptive thinking (Claude 4.6+; the workspace default model is Opus 4.8,
            # where the old {"type": "enabled", "budget_tokens": N} form is rejected).
            # Cache note: toggling thinking invalidates only the messages-tier cache,
            # never the tools+system entry.
            kwargs["thinking"] = {"type": "adaptive"}
        return kwargs

    @staticmethod
    def _to_result(response: Any) -> ChatResult:
        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.debug(
                "anthropic usage: input=%s cache_read=%s cache_write=%s output=%s",
                getattr(usage, "input_tokens", None),
                getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None),
                getattr(usage, "output_tokens", None),
            )
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

    @staticmethod
    def _mark_cache_breakpoints(wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach cache_control markers to the conversation (pure; mutates only
        dicts/lists freshly built by _to_wire, never history — marked blocks are
        replaced with copies, so signed thinking blocks that ride history by
        reference are never touched).

        Placement: the last cacheable block of the last message (the moving
        breakpoint), plus another marker each time ~_CACHE_SPACING_BLOCKS blocks
        accumulate walking backward, capped at _MAX_MESSAGE_MARKS. The spacing
        keeps every new breakpoint within the API's 20-block lookback of a
        previously cached position even across tool-heavy turns.
        """
        marks = 0
        blocks_since_mark = 0
        for msg in reversed(wire):
            content = msg.get("content")
            if isinstance(content, str):
                if not content:
                    continue
                content = [{"type": "text", "text": content}]
                msg["content"] = content
            if not content:
                continue
            wants_mark = marks == 0 or blocks_since_mark >= _CACHE_SPACING_BLOCKS
            if wants_mark and content[-1].get("type") in _CACHEABLE_TYPES:
                content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
                marks += 1
                blocks_since_mark = 0
                if marks >= _MAX_MESSAGE_MARKS:
                    break
            blocks_since_mark += len(content)
        return wire
