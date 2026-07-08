"""Tool-calling agent loop over the configured LLM provider."""

from __future__ import annotations

from typing import Callable

from quickjoiner.llm.base import AgentTool, LLMProvider, Message

MAX_TOOL_ROUNDS = 10

EventCallback = Callable[[str, str], None]
# Event types emitted to on_event:
#   "thinking"  - model reasoning delta (streamed)
#   "delta"     - answer text delta (streamed)
#   "tool_call" - the agent is invoking a tool (detail = tool name)


class OnboardingAgent:
    def __init__(self, provider: LLMProvider, tools: list[AgentTool], system: str):
        self._provider = provider
        self._tools = {t.spec.name: t for t in tools}
        self._system = system

    def ask(
        self,
        question: str,
        history: list[Message] | None = None,
        on_event: EventCallback | None = None,
    ) -> tuple[str, list[Message]]:
        """Run one user turn to completion. Returns (answer, updated history)."""
        messages: list[Message] = list(history or [])
        messages.append({"role": "user", "content": question})
        specs = [t.spec for t in self._tools.values()]

        on_stream = None
        if on_event:
            on_stream = lambda kind, delta: on_event("delta" if kind == "text" else "thinking", delta)

        for _ in range(MAX_TOOL_ROUNDS):
            result = self._provider.chat(
                messages, system=self._system, tools=specs, on_stream=on_stream
            )
            if not result.tool_calls:
                messages.append({"role": "assistant", "content": result.text})
                return result.text, messages

            assistant: Message = {
                "role": "assistant", "content": result.text, "tool_calls": result.tool_calls,
            }
            if result.thinking_blocks:
                # Signed thinking blocks must ride along so the provider can echo
                # them back on the next round of this tool-use turn.
                assistant["thinking_blocks"] = result.thinking_blocks
            messages.append(assistant)
            for call in result.tool_calls:
                if on_event:
                    on_event("tool_call", call.name)
                tool = self._tools.get(call.name)
                if tool is None:
                    output = f"Error: unknown tool {call.name!r}"
                else:
                    try:
                        output = tool.run(**call.input)
                    except Exception as exc:
                        output = f"Error running {call.name}: {exc}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": output,
                    }
                )

        fallback = "I hit the tool-call limit before finishing. Here is what I have so far."
        messages.append({"role": "assistant", "content": fallback})
        return fallback, messages
