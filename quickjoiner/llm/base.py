"""Provider-neutral chat and tool-calling primitives.

Neutral message format (list[dict]):
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": list[ToolCall]}  # tool_calls optional
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
Each provider translates this to its own wire format.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

Message = dict[str, Any]

# Streaming callback: (kind, delta) where kind is "text" or "thinking".
StreamCallback = Callable[[str, str], None]

# OpenAI's function-calling spec (and the gpt-oss "Harmony" tool-call parser behind
# many OpenAI-compatible proxies) requires tool names to match ^[a-zA-Z0-9_-]{1,64}$.
# Connector live-tool names embed the source name (e.g. "Appriver Octopus"), whose
# space would break the wire format — gpt-oss returns HTTP 500 ("unexpected tokens
# remaining in message header: to=functions.octopus_deployment_status_Appriver").
_INVALID_TOOL_NAME_CHAR = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_tool_name(name: str) -> str:
    """Coerce a tool name to the OpenAI-compatible ^[a-zA-Z0-9_-]{1,64}$ shape.

    Invalid characters (spaces, dots, …) collapse to underscores; the result is
    capped at 64 chars. Applied at ToolSpec construction so the request payload,
    the model's returned tool_call.name, and the agent's dispatch key stay identical.
    """
    cleaned = _INVALID_TOOL_NAME_CHAR.sub("_", name).strip("_")
    cleaned = re.sub(r"_{2,}", "_", cleaned)[:64].strip("_")
    return cleaned or "tool"


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        self.name = sanitize_tool_name(self.name)


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
