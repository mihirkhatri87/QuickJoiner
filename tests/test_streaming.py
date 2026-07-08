"""Streaming + thinking plumbing: agent event forwarding, wire formats, chunk folding."""

from quickjoiner.agent.agent import OnboardingAgent
from quickjoiner.llm.anthropic_provider import AnthropicProvider
from quickjoiner.llm.base import ChatResult, LLMProvider, ToolCall
from quickjoiner.llm.ollama_provider import accumulate_chunk

from tests.test_agent_loop import ScriptedProvider, _echo_tool


class StreamingScriptedProvider(LLMProvider):
    """Streams thinking then text deltas before returning each scripted result."""

    name = "streaming-scripted"

    def __init__(self, results):
        self.results = list(results)

    def chat(self, messages, system=None, tools=None, on_stream=None):
        result = self.results.pop(0)
        if on_stream:
            for chunk in ("consider", "ing..."):
                on_stream("thinking", chunk)
            for chunk in result.text.split(" "):
                on_stream("text", chunk + " ")
        return result


def test_agent_forwards_stream_events_in_order():
    provider = StreamingScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={"value": "x"})]),
            ChatResult(text="final answer"),
        ]
    )
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    events = []
    answer, _ = agent.ask("q", on_event=lambda t, d: events.append((t, d)))

    assert answer == "final answer"
    types = [t for t, _ in events]
    assert types.count("tool_call") == 1
    assert "thinking" in types and "delta" in types
    # Streamed answer deltas reassemble into the final answer.
    streamed = "".join(d for t, d in events if t == "delta")
    assert "final answer" in streamed
    # Thinking precedes the first answer delta within a model turn.
    assert types.index("thinking") < types.index("delta")


def test_agent_without_on_event_passes_no_stream():
    provider = ScriptedProvider([ChatResult(text="ok")])
    agent = OnboardingAgent(provider, [], system="sys")
    answer, _ = agent.ask("q")  # must not raise even though no on_stream is wired
    assert answer == "ok"


def test_agent_carries_thinking_blocks_through_history():
    blocks = [{"type": "thinking", "thinking": "chain of thought", "signature": "sig123"}]

    class ThinkingProvider(LLMProvider):
        name = "thinking-scripted"

        def __init__(self):
            self.calls = []

        def chat(self, messages, system=None, tools=None, on_stream=None):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                return ChatResult(
                    text="",
                    tool_calls=[ToolCall(id="c1", name="echo", input={"value": "x"})],
                    thinking="chain of thought",
                    thinking_blocks=blocks,
                )
            return ChatResult(text="done")

    provider = ThinkingProvider()
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    answer, _ = agent.ask("q")
    assert answer == "done"
    assistant_msgs = [m for m in provider.calls[1] if m["role"] == "assistant"]
    assert assistant_msgs[0]["thinking_blocks"] == blocks


def test_anthropic_wire_puts_thinking_blocks_first():
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "answer so far",
            "tool_calls": [ToolCall(id="c1", name="echo", input={})],
            "thinking_blocks": [{"type": "thinking", "thinking": "t", "signature": "s"}],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "out"},
    ]
    wire = AnthropicProvider._to_wire(messages)
    assistant = wire[1]
    kinds = [b["type"] for b in assistant["content"]]
    assert kinds == ["thinking", "text", "tool_use"]


def test_ollama_accumulate_chunk_folds_text_thinking_and_tools():
    events = []
    acc = {"text": "", "thinking": "", "tool_calls": []}
    accumulate_chunk({"thinking": "hmm "}, acc, lambda k, d: events.append((k, d)))
    accumulate_chunk({"content": "The answer "}, acc, lambda k, d: events.append((k, d)))
    accumulate_chunk(
        {"content": "is 42.", "tool_calls": [{"function": {"name": "echo", "arguments": {}}}]},
        acc,
        lambda k, d: events.append((k, d)),
    )
    assert acc["text"] == "The answer is 42." and acc["thinking"] == "hmm "
    assert len(acc["tool_calls"]) == 1
    assert events[0] == ("thinking", "hmm ") and events[1][0] == "text"
