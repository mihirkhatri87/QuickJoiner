"""Provider-neutral chat and tool-calling primitives.

Neutral message format (list[dict]):
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": list[ToolCall]}  # tool_calls optional
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
Each provider translates this to its own wire format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

Message = dict[str, Any]

# Streaming callback: (kind, delta) where kind is "text" or "thinking".
StreamCallback = Callable[[str, str], None]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    thinking: str = ""  # model reasoning text, when the provider exposes it
    # Provider-specific opaque thinking payloads (e.g. Anthropic signed thinking
    # blocks) that must be echoed back on the next turn; carried on the assistant
    # history message as "thinking_blocks".
    thinking_blocks: list[Any] = field(default_factory=list)


@dataclass
class AgentTool:
    """A ToolSpec paired with the Python callable that executes it."""

    spec: ToolSpec
    fn: Callable[..., str]

    def run(self, **kwargs: Any) -> str:
        return self.fn(**kwargs)


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ChatResult:
        """Run one model turn. Providers must support tool calling.

        When on_stream is given, providers that can stream call it with
        ("text"|"thinking", delta) as tokens arrive; the full ChatResult is
        still returned at the end either way.
        """
